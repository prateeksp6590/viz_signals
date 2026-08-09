"""Rolling per-instrument market data view handed to strategies.

Exposes raw ticks, on-demand OHLCV bars, and the greeks/IV series over the
configured lookback window. Refreshed incrementally each poll cycle.
"""

from datetime import datetime, time as dt_time, timedelta, timezone

import pandas as pd

from ..config import settings
from ..utils.logger import logger
from .influx_reader import InfluxReader

GREEK_FIELDS = ['iv', 'delta', 'theta', 'gamma', 'vega', 'rho']


class InstrumentView:
    def __init__(self, instrument_key: str, symbol: str):
        self.instrument_key = instrument_key
        self.symbol = symbol
        self.ticks: pd.DataFrame = pd.DataFrame()
        self.position = None          # set by the engine each cycle (Position | None)
        self._bar_cache: dict[str, pd.DataFrame] = {}

    @property
    def ltp(self) -> float | None:
        if self.ticks.empty or 'ltp' not in self.ticks.columns:
            return None
        s = self.ticks['ltp'].dropna()
        return float(s.iloc[-1]) if not s.empty else None

    @property
    def last_ts(self) -> datetime | None:
        return self.ticks.index[-1].to_pydatetime() if not self.ticks.empty else None

    def bars(self, interval: str = '1min') -> pd.DataFrame:
        """OHLCV bars resampled from ticks. Volume from cumulative-vtt diffs
        (0 for index instruments, which carry no volume)."""
        if interval in self._bar_cache:
            return self._bar_cache[interval]
        if self.ticks.empty or 'ltp' not in self.ticks.columns:
            return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

        ltp = self.ticks['ltp'].dropna()
        bars = ltp.resample(interval).ohlc().dropna(how='all')
        if 'vtt' in self.ticks.columns and self.ticks['vtt'].notna().any():
            vtt_last = self.ticks['vtt'].dropna().resample(interval).last()
            vol = vtt_last.diff()
            if not vol.empty:
                first = vtt_last.index[0]
                window = self.ticks['vtt'].dropna()[:first + pd.Timedelta(interval)]
                vol.iloc[0] = window.iloc[-1] - window.iloc[0] if len(window) > 1 else 0
            bars['volume'] = vol.reindex(bars.index).clip(lower=0).fillna(0)
        else:
            bars['volume'] = 0.0

        self._bar_cache[interval] = bars
        return bars

    @property
    def greeks(self) -> pd.DataFrame:
        cols = [c for c in GREEK_FIELDS if c in self.ticks.columns]
        if not cols:
            return pd.DataFrame(columns=GREEK_FIELDS)
        return self.ticks[cols].dropna(how='all')

    def _append(self, new: pd.DataFrame) -> None:
        if new.empty:
            self._trim()
            return
        self.ticks = new if self.ticks.empty else pd.concat([self.ticks, new])
        self._trim()
        self._bar_cache.clear()

    def _trim(self) -> None:
        if self.ticks.empty:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.LOOKBACK_MINUTES)
        self.ticks = self.ticks[self.ticks.index >= cutoff]


class MarketData:
    """Owns all InstrumentViews; refreshes them from InfluxDB each cycle."""

    def __init__(self, reader: InfluxReader, symbol_map: dict[str, str]):
        self._reader = reader
        def _excluded(key: str) -> bool:
            sym = (symbol_map.get(key) or key).upper()
            return any(x in sym for x in settings.ANALYZE_EXCLUDE_SYMBOLS)

        dropped = [k for k in settings.ANALYZE_INSTRUMENTS if _excluded(k)]
        if dropped:
            # loud on purpose: silently analysing fewer instruments than the feeder
            # collects is the kind of mismatch that goes unnoticed for days
            logger.info(f'excluding {len(dropped)} instrument(s) from analysis by '
                        f'ANALYZE_EXCLUDE_SYMBOLS='
                        f'{",".join(settings.ANALYZE_EXCLUDE_SYMBOLS)}: '
                        f'{[symbol_map.get(k, k) for k in dropped][:5]}')
        self.views: dict[str, InstrumentView] = {
            key: InstrumentView(key, symbol_map.get(key, key))
            for key in settings.ANALYZE_INSTRUMENTS if not _excluded(key)
        }

    @staticmethod
    def _exchange_open(instrument_key: str) -> bool:
        """False once that exchange has closed for the day.

        NSE/BSE stop at 15:30 while MCX runs to 23:30. Without this, a single engine
        covering both would keep querying dead NSE instruments all evening and — worse
        — their frozen views would keep satisfying the strategy on stale geometry.
        """
        exch = instrument_key.split('|', 1)[0].split('_', 1)[0].upper()
        hhmm = settings.EXCHANGE_CLOSE.get(exch)
        if not hhmm:
            return True
        try:
            h, m = (int(x) for x in hhmm.split(':'))
        except ValueError:
            return True
        return datetime.now(timezone(timedelta(hours=5, minutes=30))).time() <= dt_time(h, m)

    def refresh(self) -> None:
        """One batched query per cycle for every OPEN instrument."""
        keys = [k for k in self.views if self._exchange_open(k)]
        if not keys:
            return
        since_map = {k: self.views[k].last_ts for k in keys}
        fetched = self._reader.fetch_many(keys, since_map)
        for key in keys:
            self.views[key]._append(fetched.get(key, pd.DataFrame()))
