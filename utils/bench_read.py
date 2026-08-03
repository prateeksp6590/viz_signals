"""
Where does the read cycle actually spend its time?

    python utils/bench_read.py

Times the identical Flux query three ways: the raw HTTP call, the client's
query_data_frame() (CSV -> pandas inside influxdb-client), and our own CSV parse.
Then times the real fetch_many() so you can see batch count and total.
"""
import sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()
import pandas as pd
from influxdb_client import InfluxDBClient
from src.config import settings
from src.services.influx_reader import InfluxReader

IST = timezone(timedelta(hours=5, minutes=30))
fields = settings.INFLUX_FIELDS or ['ltp']
fcond = ' or '.join(f'r._field == "{f}"' for f in fields)
FLUX = f'''
from(bucket: "{settings.INFLUX_BUCKET}")
  |> range(start: -30s)
  |> filter(fn: (r) => {fcond})
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''

def timed(label, fn, n=3):
    best, out = None, None
    for _ in range(n):
        t0 = time.perf_counter(); out = fn(); dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    size = len(out) if hasattr(out, '__len__') else '?'
    print(f'  {label:38}{best*1000:8.0f} ms   rows/chars {size}')
    return best

def main():
    print(f'bucket={settings.INFLUX_BUCKET} fields={fields} '
          f'chunk={settings.INFLUX_QUERY_CHUNK} workers={settings.INFLUX_QUERY_WORKERS}')
    print(f'instruments configured: {len(settings.ANALYZE_INSTRUMENTS)}\n')
    c = InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                       org=settings.INFLUX_ORG, timeout=60_000)
    qa = c.query_api()
    print('same query, three ways (best of 3):')
    t_raw = timed('query_raw (HTTP + CSV text)', lambda: qa.query_raw(FLUX).data.decode())
    t_df  = timed('query_data_frame (client parser)', lambda: qa.query_data_frame(FLUX))
    def ours():
        import io
        txt = qa.query_raw(FLUX).data.decode()
        return pd.read_csv(io.StringIO(txt), comment='#').dropna(how='all')
    t_own = timed('query_raw + pandas.read_csv', ours)
    if t_df > t_raw * 2:
        print(f'\n  -> the CLIENT PARSER is the bottleneck '
              f'({(t_df-t_raw)*1000:.0f} ms of {t_df*1000:.0f} ms). '
              f'Our own parse is {t_df/t_own:.1f}x faster.')
    else:
        print('\n  -> parsing is not dominant; the cost is the query itself.')

    print('\nreal fetch_many() across every configured instrument:')
    r = InfluxReader({})
    try:
        import json as _j
        smap = {}
        for ex in ('NSE', 'BSE', 'MCX'):
            p = settings.NSE_JSON_PATH.parent / f'{ex}.json'
            if p.exists():
                ks = set(settings.ANALYZE_INSTRUMENTS)
                for row in _j.loads(p.read_text()):
                    if row.get('instrument_key') in ks:
                        smap[row['instrument_key']] = row['trading_symbol']
        r._symbol_map = smap
        now = datetime.now(timezone.utc)
        since = {k: now - timedelta(seconds=30) for k in settings.ANALYZE_INSTRUMENTS}
        t0 = time.perf_counter(); got = r.fetch_many(settings.ANALYZE_INSTRUMENTS, since)
        dt = time.perf_counter() - t0
        rows = sum(len(v) for v in got.values())
        print(f'  {len(settings.ANALYZE_INSTRUMENTS)} instruments -> {len(got)} with data, '
              f'{rows:,} rows in {dt*1000:.0f} ms')
    finally:
        r.close(); c.close()

if __name__ == '__main__':
    main()
