#!/usr/bin/env python3
"""1-minute OHLC poller — REST, independent of the websocket feeder.

    python utils/poll_ohlc.py --dry-run --once      # see one poll, write nothing
    python utils/poll_ohlc.py                       # run the session

WHY THIS EXISTS
---------------
On 2026-08-14 vizsignals ran six clean hours against data that never arrived: the
feeder had subscribed to expired contracts, so the tick buffers stayed empty, the
engine polled them happily, and it exited with "0 sent, 0 failed". Nothing failed
loudly because nothing failed — the strategy was reading a store that simply had
nothing in it.

A component that fetches its OWN data cannot do that. If the API returns nothing,
this process knows on the first call, says so, and exits non-zero so the systemd
OnFailure alert fires. That property, not simplicity, is the reason to run it.

The websocket feeder keeps its job unchanged: capture everything at tick level for
research. This is the DECISION path, and it is deliberately allowed to be poorer
and slower in exchange for being verifiable.

THREE THINGS THE UPSTOX API MAKES EASY THAT ARE EASY TO GET WRONG
-----------------------------------------------------------------
1. USE prev_ohlc, NEVER live_ohlc. `live_ohlc` is the candle currently forming; its
   OHLC changes on every poll, so acting on it means acting on a bar that does not
   exist yet. `prev_ohlc` is the completed minute. This is the same trap that
   ohlc_slope.py avoids by discarding bars.iloc[-1] — here the API hands over the
   completed bar directly, with a TRUE high and low rather than a single sampled
   price.

   (Polling LTP once a minute and calling it a candle gives open==high==low==close.
   Any strategy using (O+H+L+C)/4 would silently degrade to the close.)

2. THE RESPONSE KEY IS NOT THE INSTRUMENT KEY YOU SENT. You request
   `BSE_FO|1234567` and the response is keyed `BSE_FO:SENSEX2582077500CE`, with the
   original in the `instrument_token` field. Matching on the dict key silently drops
   every instrument.

3. `ts` IS THE CANDLE START. Stored here at the candle's RIGHT edge (ts + 60s),
   because that is the convention the rest of this codebase uses after
   `label='right', closed='right'` fixed a fake 84% hit rate that came from bars
   containing ticks stamped after their own label. `ts_start` is kept as a field so
   nothing is lost.

RATE LIMITS: 25 req/sec outside websockets, and up to 500 instrument keys per
request. ~22 instruments is ONE request per minute — about 0.07% of the budget.
Chunking defaults to 100 to keep the URL a sane length, not because of throughput.
"""

import argparse
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import requests

# MUST be first: settings.py reads os.environ and never loads .env, so without this
# the script runs on built-in defaults with an empty token and 401s on every call.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                             # noqa: E402,F401

from src.config import settings                              # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OHLC_URL = 'https://api.upstox.com/v3/market-quote/ohlc'
GREEK_URL = 'https://api.upstox.com/v3/market-quote/option-greek'

# 15:40 since 2026-08-03 (Closing Auction Session). See influx_writer.NSE_BSE_CLOSE.
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 40)
VIX_KEY = 'NSE_INDEX|India VIX'

POLL_OFFSET_SECS = 5      # poll at :05 past the minute so prev_ohlc is settled
MAX_CONSECUTIVE_FAIL = 5  # then exit non-zero and let OnFailure alert


def _now() -> datetime:
    return datetime.now(IST)


def market_open_now() -> bool:
    n = _now()
    return n.weekday() < 5 and MARKET_OPEN <= n.time() <= MARKET_CLOSE


class OhlcPoller:
    def __init__(self, keys, token, symbol_map=None, chunk=100, interval='I1',
                 session=None):
        self.keys = list(dict.fromkeys(keys))       # dedupe, keep order
        self.token = token
        self.symbol_map = symbol_map or {}
        self.chunk = max(1, chunk)
        self.interval = interval
        self.http = session or requests.Session()
        self._last_ts: dict[str, int] = {}
        self.gaps: list[str] = []

    # ---------------------------------------------------------------- transport
    def _get(self, url: str, keys: list) -> dict:
        r = self.http.get(
            url,
            params={'instrument_key': ','.join(keys), 'interval': self.interval},
            headers={'Accept': 'application/json',
                     'Authorization': f'Bearer {self.token}'},
            timeout=20)
        if r.status_code == 401:
            raise SystemExit('Upstox returned 401 — the access token is expired or '
                             'missing. Regenerate it and restart.')
        r.raise_for_status()
        body = r.json()
        if body.get('status') != 'success':
            raise RuntimeError(f'API status={body.get("status")}: {str(body)[:300]}')
        return body.get('data') or {}

    # ------------------------------------------------------------------ parsing
    def parse(self, data: dict) -> list:
        """API payload -> completed candles, deduped, with gaps recorded.

        Keyed off `instrument_token`, NOT the dict key: the response is keyed by
        trading symbol (BSE_FO:SENSEX2582077500CE) while the key we asked for is
        BSE_FO|1234567. Matching on the dict key drops everything.
        """
        out = []
        for _resp_key, node in (data or {}).items():
            if not isinstance(node, dict):
                continue
            key = node.get('instrument_token')
            candle = node.get('prev_ohlc')          # NEVER live_ohlc
            if not key or not isinstance(candle, dict):
                continue
            ts = candle.get('ts')
            close = candle.get('close')
            if ts is None or close is None:
                continue
            ts = int(ts)

            prev = self._last_ts.get(key)
            if prev is not None:
                if ts <= prev:
                    continue                        # same candle polled twice
                missed = (ts - prev) // 60_000 - 1
                if missed > 0:
                    self.gaps.append(f'{self.name_of(key)}: {missed} minute(s) '
                                     f'missing before '
                                     f'{datetime.fromtimestamp(ts / 1000, IST):%H:%M}')
            self._last_ts[key] = ts

            out.append({
                'instrument_key': key,
                'symbol': self.name_of(key),
                'measurement': self.measurement_of(key),
                'ts_start': ts,
                # right edge: the candle [ts, ts+60s) is labelled at its END
                'time_ns': (ts + 60_000) * 1_000_000,
                'open': float(candle.get('open') or close),
                'high': float(candle.get('high') or close),
                'low': float(candle.get('low') or close),
                'close': float(close),
                'volume': float(candle.get('volume') or 0.0),
                'last_price': float(node.get('last_price') or close),
            })
        return out

    def name_of(self, key: str) -> str:
        return self.symbol_map.get(key) or key

    def measurement_of(self, key: str) -> str:
        """BSE_FO|1234567 -> 'BSE_SENSEX 77500 CE 20 AUG 26'.

        With no symbol map, falls back to '{segment}_{token}' (BSE_FO_1234567) to
        match viz_hedge's documented fallback. Naively prefixing the exchange to the
        raw key produced 'BSE_BSE_FO|1234567', which matches nothing on either side.
        """
        sym = self.symbol_map.get(key)
        if sym:
            return f"{str(key).split('|')[0].split('_')[0]}_{sym}"
        seg, _, tok = str(key).partition('|')
        return f'{seg}_{tok}' if tok else str(key)

    def poll_once(self) -> list:
        rows = []
        for i in range(0, len(self.keys), self.chunk):
            batch = self.keys[i:i + self.chunk]
            rows.extend(self.parse(self._get(OHLC_URL, batch)))
            if i + self.chunk < len(self.keys):
                time.sleep(0.05)                    # nowhere near 25/s, but be polite
        return rows


# ------------------------------------------------------------------- persistence
def write_influx(rows: list, bucket: str) -> int:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    pts = []
    for r in rows:
        p = (Point(r['measurement'])
             .tag('instrument_key', r['instrument_key'])
             .tag('segment', str(r['instrument_key']).split('|')[0])
             .tag('source', 'poller')
             .field('open', r['open']).field('high', r['high'])
             .field('low', r['low']).field('close', r['close'])
             .field('volume', r['volume']).field('ts_start', float(r['ts_start']))
             .time(r['time_ns'], WritePrecision.NS))
        pts.append(p)
    if not pts:
        return 0
    with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                        org=settings.INFLUX_ORG, timeout=60_000) as c:
        c.write_api(write_options=SYNCHRONOUS).write(bucket=bucket,
                                                     org=settings.INFLUX_ORG,
                                                     record=pts)
    return len(pts)


def load_token() -> str:
    import os
    tok = os.getenv('UPSTOX_ACCESS_TOKEN', '').strip()
    if tok:
        return tok
    # fall back to viz_hedge's daily cache, so both processes share one token
    cache = Path(os.getenv('UPSTOX_TOKEN_DIR',
                           str(Path.home() / 'viz_hedge' / 'tokens')))
    f = cache / f'acc_tk_{_now():%Y%m%d}'
    if f.exists():
        return f.read_text(encoding='utf-8').strip()
    raise SystemExit(f'No token: set UPSTOX_ACCESS_TOKEN or provide {f}')


def load_keys(args) -> list:
    keys = []
    if args.instruments:
        keys = [k.strip() for k in args.instruments.split(',') if k.strip()]
    else:
        keys = list(getattr(settings, 'ANALYZE_INSTRUMENTS', []) or [])
    if args.vix:
        keys.append(VIX_KEY)
    if not keys:
        raise SystemExit('No instruments. Set ANALYZE_INSTRUMENTS or pass '
                         '--instruments.')
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--instruments', help='comma-separated keys (default: '
                                          'ANALYZE_INSTRUMENTS)')
    ap.add_argument('--bucket', default='ohlc_1m')
    ap.add_argument('--chunk', type=int, default=100)
    ap.add_argument('--vix', action='store_true', default=True,
                    help='include NSE_INDEX|India VIX (default on)')
    ap.add_argument('--no-vix', dest='vix', action='store_false')
    ap.add_argument('--once', action='store_true', help='one poll, then exit')
    ap.add_argument('--dry-run', action='store_true', help='print, do not write')
    ap.add_argument('--ignore-hours', action='store_true')
    a = ap.parse_args()

    keys = load_keys(a)
    smap = {}
    try:
        from src.main import _load_symbol_map
        smap = _load_symbol_map() or {}
    except Exception as e:
        print(f'  symbol map unavailable ({e}); measurements will use raw keys')

    poller = OhlcPoller(keys, load_token(), smap, chunk=a.chunk)
    print(f'poll_ohlc: {len(keys)} instruments, bucket={a.bucket}, '
          f'{"DRY RUN" if a.dry_run else "writing"}, '
          f'session {MARKET_OPEN:%H:%M}-{MARKET_CLOSE:%H:%M} IST')

    fails = 0
    while True:
        if not (a.ignore_hours or market_open_now()):
            if a.once:
                print('  market closed — use --ignore-hours to poll anyway')
                return 0
            n = _now()
            if n.time() > MARKET_CLOSE or n.weekday() >= 5:
                print(f'  session over at {n:%H:%M:%S} IST — stopping cleanly')
                return 0
            time.sleep(20)
            continue

        try:
            rows = poller.poll_once()
            if not rows:
                fails += 1
                print(f'  {_now():%H:%M:%S}  NO CANDLES ({fails}/{MAX_CONSECUTIVE_FAIL})',
                      file=sys.stderr)
            else:
                fails = 0
                n = 0 if a.dry_run else write_influx(rows, a.bucket)
                stamp = datetime.fromtimestamp(rows[0]['ts_start'] / 1000, IST)
                print(f'  {_now():%H:%M:%S}  candle {stamp:%H:%M}  '
                      f'{len(rows)} instruments'
                      + (f'  wrote {n}' if not a.dry_run else '  (dry run)'))
                if a.dry_run:
                    for r in rows[:5]:
                        print(f'      {r["symbol"][:30]:<30} O {r["open"]:>9.2f} '
                              f'H {r["high"]:>9.2f} L {r["low"]:>9.2f} '
                              f'C {r["close"]:>9.2f} V {r["volume"]:>10,.0f}')
                while poller.gaps:
                    print(f'  GAP: {poller.gaps.pop(0)}', file=sys.stderr)
        except SystemExit:
            raise
        except Exception as e:
            fails += 1
            print(f'  {_now():%H:%M:%S}  poll failed ({fails}/'
                  f'{MAX_CONSECUTIVE_FAIL}): {str(e)[:200]}', file=sys.stderr)

        # Exit non-zero rather than looping quietly. A poller that keeps running
        # while returning nothing is the exact failure this component exists to
        # remove -- systemd OnFailure turns this into a Telegram message.
        if fails >= MAX_CONSECUTIVE_FAIL:
            print(f'{MAX_CONSECUTIVE_FAIL} consecutive failures — exiting non-zero '
                  f'so the alert fires', file=sys.stderr)
            return 1

        if a.once:
            return 0

        now = time.time()
        time.sleep(max(1.0, 60.0 - (now % 60.0) + POLL_OFFSET_SECS))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
