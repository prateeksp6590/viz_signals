"""
One poller, many clients.

Each connected browser must NOT poll InfluxDB itself: with a phone, a laptop and a
tablet open you would triple the query load for identical data. A single background
task polls once per interval and fans the result out to every subscriber — the same
reason the signal engine batches its reads.

Ticks are pushed as deltas (only rows newer than the last watermark), so a client
that stays connected receives a continuous stream rather than repeated snapshots.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.services.influx_reader import InfluxReader
from src.utils.logger import logger

IST = timezone(timedelta(hours=5, minutes=30))


class Hub:
    def __init__(self, symbol_map: dict[str, str], interval: float = 1.0,
                 bar_secs: int = 30, raw_ticks: bool = False):
        self._reader = InfluxReader(symbol_map)
        self._symbol_map = symbol_map
        self._interval = interval
        self._subs: set[asyncio.Queue] = set()
        self._watermark: dict[str, datetime] = {}
        self._sig_watermark = datetime.now(timezone.utc) - timedelta(minutes=5)
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None
        # Stream OHLC BARS, not raw ticks: 50 instruments x ~1.5 ticks/s is ~75
        # messages/second to every device, which is pointless on a phone when the UI
        # draws candles anyway. The bar currently forming is re-sent each poll with
        # closed=false, so the display stays live at ~1/30th the traffic.
        # NOTE: the signal engine still reads RAW ticks -- n1/n2 are tick counts and
        # 2s of added latency costs ~61% of the strategy's P&L.
        self._bar_secs = max(1, int(bar_secs))
        self._raw_ticks = raw_ticks
        self._bars: dict[str, dict] = {}

    # ── subscriptions ────────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _broadcast(self, msg: dict) -> None:
        dead = []
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # a slow client must never stall the poller or the others
                dead.append(q)
        for q in dead:
            self._subs.discard(q)
            logger.warning('hub: dropped a slow subscriber')

    @property
    def n_clients(self) -> int:
        return len(self._subs)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._reader.close()

    async def _loop(self) -> None:
        keys = settings.ANALYZE_INSTRUMENTS
        while True:
            try:
                if self._subs:                       # no clients -> no queries
                    await asyncio.to_thread(self._poll, keys)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = ' '.join(str(e).split())[:200]
                logger.error(f'hub poll failed: {self.last_error}')
            await asyncio.sleep(self._interval)

    def _bucket(self, ts) -> int:
        """Epoch seconds of the bar this timestamp belongs to."""
        return int(ts.timestamp()) // self._bar_secs * self._bar_secs

    def _poll(self, keys: list[str]) -> None:
        got = self._reader.fetch_many(keys, dict(self._watermark))
        ticks, updated = [], {}
        for k, df in got.items():
            if df.empty or 'ltp' not in df:
                continue
            self._watermark[k] = df.index[-1].to_pydatetime()
            sym = self._symbol_map.get(k, k)
            for ts, row in df.iterrows():
                px = float(row['ltp'])
                if px != px:
                    continue
                b = self._bucket(ts)
                cur = self._bars.get(k)
                if cur is None or cur['b'] != b:
                    if cur is not None:
                        cur['closed'] = True
                        updated[(k, cur['b'])] = dict(cur)   # emit the finished bar
                    cur = {'key': k, 'symbol': sym, 'b': b, 'o': px, 'h': px,
                           'l': px, 'c': px, 'v': 0.0, 'n': 0, 'closed': False}
                    self._bars[k] = cur
                cur['h'] = max(cur['h'], px)
                cur['l'] = min(cur['l'], px)
                cur['c'] = px
                cur['n'] += 1
                updated[(k, cur['b'])] = dict(cur)
                if self._raw_ticks:
                    ticks.append({'key': k, 'symbol': sym,
                                  't': ts.astimezone(IST).isoformat(), 'ltp': px})

        if updated:
            out = []
            for (_k, b), bar in updated.items():
                out.append({'key': bar['key'], 'symbol': bar['symbol'],
                            't': datetime.fromtimestamp(b, IST).isoformat(),
                            'o': bar['o'], 'h': bar['h'], 'l': bar['l'], 'c': bar['c'],
                            'ticks': bar['n'], 'closed': bar['closed']})
            self._broadcast({'type': 'bars', 'secs': self._bar_secs, 'data': out})
        if ticks:
            self._broadcast({'type': 'ticks', 'data': ticks})

        for s in self._recent_signals():
            self._broadcast({'type': 'signal', 'data': s})

    def _recent_signals(self) -> list[dict]:
        """New rows from the signals bucket since the last poll."""
        start = self._sig_watermark.isoformat().replace('+00:00', 'Z')
        flux = (f'from(bucket:"{settings.SIGNALS_BUCKET}")\n'
                f'  |> range(start: {start})\n'
                f'  |> filter(fn: (r) => r._measurement == "signal")\n'
                f'  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")')
        try:
            df = self._reader._query_api.query_data_frame(flux)
        except Exception:
            return []
        import pandas as pd
        if isinstance(df, list):
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
        if df is None or df.empty or '_time' not in df.columns:
            return []
        df['_time'] = pd.to_datetime(df['_time'], utc=True)
        df = df[df['_time'] > self._sig_watermark]
        if df.empty:
            return []
        self._sig_watermark = df['_time'].max().to_pydatetime()
        out = []
        for _, r in df.iterrows():
            out.append({'t': r['_time'].astimezone(IST).isoformat(),
                        'symbol': r.get('symbol'), 'action': r.get('action'),
                        'price': float(r['price']) if 'price' in r else None,
                        'angle': float(r['angle_deg']) if 'angle_deg' in r and
                                 r['angle_deg'] == r['angle_deg'] else None,
                        'threshold': float(r['threshold_deg']) if 'threshold_deg' in r and
                                     r['threshold_deg'] == r['threshold_deg'] else None})
        return out
