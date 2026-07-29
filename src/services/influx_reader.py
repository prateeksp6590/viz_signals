"""Reads tick data written by viz_hedge.

Schema (current -- matches viz_hedge/src/services/influx_writer.py):
  bucket      : ONE shared bucket, settings.INFLUX_BUCKET (default 'tick_data')
  measurement : {EXCH}_{trading_symbol}   e.g. 'NSE_HDFCBANK',
                'BSE_SENSEX 77000 CE 30 JUL 26'   -- NO _YYYYMMDD suffix
                falls back to instrument_key with '|' -> '_' if unmapped
  tags        : segment, exch, symbol
Days are separated by the point timestamp and the query time range, not by the
bucket or measurement name.
"""

from datetime import datetime, timezone

import pandas as pd
from influxdb_client import InfluxDBClient

from ..config import settings
from ..utils.logger import logger

TICK_FIELDS = ['ltp', 'ltq', 'vtt', 'oi', 'tbq', 'tsq',
               'iv', 'delta', 'theta', 'gamma', 'vega', 'rho']

_META_COLS = ['result', 'table', '_start', '_stop', '_measurement',
              'segment', 'exch', 'symbol']


def _flux_escape(v: str) -> str:
    return v.replace('\\', '\\\\').replace('"', '\\"')


class InfluxReader:
    def __init__(self, symbol_map: dict[str, str]):
        self._client = InfluxDBClient(
            url=settings.INFLUX_URL,
            token=settings.INFLUX_TOKEN,
            org=settings.INFLUX_ORG,
            timeout=settings.INFLUX_QUERY_TIMEOUT_MS,
        )
        self._query_api = self._client.query_api()
        self._symbol_map = symbol_map

    def bucket_name(self) -> str:
        return settings.INFLUX_BUCKET

    @staticmethod
    def exchange(instrument_key: str) -> str:
        # 'NSE_FO|72272' -> 'NSE';  'BSE_FO|1234' -> 'BSE'
        return instrument_key.split('|', 1)[0].split('_', 1)[0]

    def measurement_name(self, instrument_key: str) -> str:
        symbol = self._symbol_map.get(instrument_key)
        if symbol:
            return f'{self.exchange(instrument_key)}_{symbol}'
        return instrument_key.replace('|', '_')

    # ── queries ───────────────────────────────────────────────────────────────

    @staticmethod
    def _field_filter(fields=None) -> str:
        """Restrict to the fields we read. Cuts rows scanned (billed) and keeps the
        pivot small enough not to trip the HTTP read timeout."""
        f = settings.INFLUX_FIELDS if fields is None else fields
        if not f:
            return ''
        conds = ' or '.join(f'r._field == "{_flux_escape(x)}"' for x in f)
        return f'  |> filter(fn: (r) => {conds})\n'

    def _query(self, measurement: str, start: str, stop: str | None = None,
               fields=None) -> pd.DataFrame:
        rng = f'start: {start}' + (f', stop: {stop}' if stop else '')
        flux = f'''
from(bucket: "{_flux_escape(self.bucket_name())}")
  |> range({rng})
  |> filter(fn: (r) => r._measurement == "{_flux_escape(measurement)}")
{self._field_filter(fields)}  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        try:
            df = self._query_api.query_data_frame(flux)
        except Exception as e:
            logger.error(f'Influx query failed for {measurement}: {e}')
            return pd.DataFrame()
        if isinstance(df, list):
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
        if df is None or df.empty or '_time' not in df.columns:
            return pd.DataFrame()
        df = df.drop(columns=[c for c in _META_COLS if c in df.columns])
        return df.set_index('_time').sort_index()

    def fetch(self, instrument_key: str, since: datetime | None) -> pd.DataFrame:
        """Ticks newer than `since` (exclusive), else the full lookback window."""
        if since is not None:
            start = since.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        else:
            start = f'-{settings.LOOKBACK_MINUTES}m'
        df = self._query(self.measurement_name(instrument_key), start)
        if since is not None and not df.empty:
            df = df[df.index > since]
        return df

    def fetch_many(self, instrument_keys: list[str],
                   since_map: dict[str, datetime]) -> dict[str, pd.DataFrame]:
        """ONE Flux query for every instrument, split by measurement afterwards.

        InfluxDB Cloud bills per query execution ($0.012 / 100), so polling N
        instruments individually costs N x more than it needs to: at 100
        instruments and a 5s loop that is 450k queries/day instead of 4.5k.
        Range starts at the OLDEST `since` and each instrument is trimmed to its
        own watermark afterwards.
        """
        if not instrument_keys:
            return {}
        meas_of = {k: self.measurement_name(k) for k in instrument_keys}
        # Use the OLDEST watermark we have. Requiring *every* instrument to have one
        # meant a single permanently-quiet leg (an illiquid MCX strike that never
        # ticks) forced a full LOOKBACK_MINUTES re-fetch of all instruments on every
        # cycle -- which defeats the point of batching, since InfluxDB bills per query
        # AND per byte scanned.
        seen = [since_map[k] for k in instrument_keys if since_map.get(k) is not None]
        if seen:
            start = min(seen).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        else:
            start = f'-{settings.LOOKBACK_MINUTES}m'      # cold start: no watermarks yet

        # chunk the measurement set: one giant pivoted query times out
        meas_list = sorted(set(meas_of.values()))
        chunk = max(1, settings.INFLUX_QUERY_CHUNK)
        frames = []
        for i in range(0, len(meas_list), chunk):
            batch = meas_list[i:i + chunk]
            wanted = ', '.join(f'"{_flux_escape(m)}"' for m in batch)
            flux = f"""
from(bucket: "{_flux_escape(self.bucket_name())}")
  |> range(start: {start})
  |> filter(fn: (r) => contains(value: r._measurement, set: [{wanted}]))
{self._field_filter()}  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
            try:
                df = self._query_api.query_data_frame(flux)
            except Exception as e:
                logger.error(f'Batched influx query failed '
                             f'(chunk {i // chunk + 1}, {len(batch)} measurements): {e}')
                continue
            if isinstance(df, list):
                df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
            if df is not None and not df.empty and '_time' in df.columns:
                frames.append(df)

        if not frames:
            return {}
        df = pd.concat(frames, ignore_index=True)
        df = df.drop(columns=[c for c in _META_COLS if c in df.columns and c != '_measurement'])
        df = df.set_index('_time').sort_index()
        by_meas = {m: g.drop(columns=['_measurement']) for m, g in df.groupby('_measurement')}

        out: dict[str, pd.DataFrame] = {}
        for key in instrument_keys:
            g = by_meas.get(meas_of[key])
            if g is None or g.empty:
                continue
            since = since_map.get(key)
            out[key] = g[g.index > since] if since is not None else g
        return out

    def fetch_range(self, instrument_key: str, start: datetime, stop: datetime) -> pd.DataFrame:
        """Explicit window -- used by the backtester to replay a past day."""
        return self._query(
            self.measurement_name(instrument_key),
            start.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
            stop.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        )

    def list_measurements(self, days: int = 1) -> list[str]:
        """Every measurement present in the bucket over the last `days` days."""
        flux = f'''
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{_flux_escape(self.bucket_name())}", start: -{days}d)
'''
        try:
            df = self._query_api.query_data_frame(flux)
        except Exception as e:
            logger.error(f'measurement listing failed: {e}')
            return []
        if isinstance(df, list):
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
        return sorted(df['_value'].tolist()) if df is not None and '_value' in df else []

    def close(self) -> None:
        self._client.close()
