#!/usr/bin/env python3
"""Split one engine cycle into its parts and time each.

WHY THIS EXISTS. `bench_read.py` reports ~300ms while the live engine reports
`influx read 9.5s` for the same 99 instruments. bench is not representative: it
passes `since = now - 30s` for EVERY instrument, so it only ever measures the
freshest age band and a tiny result set. The live engine passes each view's real
`last_ts`, which is None until that view has data -- and a None watermark falls to
the `cold` branch, i.e. a full LOOKBACK_MINUTES scan for that instrument.

This script reproduces `MarketData.refresh()` exactly and separates:
    query time   (InfluxReader.fetch_many)
    append time  (pd.concat + _trim across every view)
so the next change is aimed at whichever one is actually large.

READ-ONLY. Safe to run while the engine is live; it opens its own reader and
never writes. It does hold its own copy of the tick buffers, so expect it to use
a few hundred MB -- do not run it during market hours on a memory-tight box.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                        # noqa: E402,F401

from src.config import settings                        # noqa: E402
from src.main import _load_symbol_map                   # noqa: E402
from src.services.influx_reader import InfluxReader    # noqa: E402
from src.services.market_view import MarketData        # noqa: E402

CYCLES = 4
GAP_SECS = 3
IST = timezone(timedelta(hours=5, minutes=30))


def main() -> int:
    ap = argparse.ArgumentParser(description='time one engine cycle, split by phase')
    ap.add_argument('--all', action='store_true',
                    help='ignore exchange hours (needed after 15:30, when '
                         '_exchange_open leaves only MCX and the sample is useless)')
    args = ap.parse_args()
    print(f'instruments={len(settings.ANALYZE_INSTRUMENTS)} '
          f'fields={settings.INFLUX_FIELDS} '
          f'lookback={settings.LOOKBACK_MINUTES}m '
          f'chunk={settings.INFLUX_QUERY_CHUNK} '
          f'workers={settings.INFLUX_QUERY_WORKERS} '
          f'filter_by={getattr(settings, "INFLUX_FILTER_BY", "?")}')
    print('\ncycle 0 is COLD (every watermark is None -> full lookback scan).\n'
          'The live engine is warm, so compare cycles 1+ against its 9.5s.\n')

    # the measurement names depend on the symbol map, so build the real one --
    # an empty map silently falls back to different measurements and would time
    # queries that the engine never issues
    try:
        smap = _load_symbol_map()
    except Exception as e:
        print(f'  could not load symbol map ({e}); falling back to empty')
        smap = {}
    reader = InfluxReader(smap)
    market = MarketData(reader, smap)

    if not settings.INFLUX_TOKEN:
        print('\n  INFLUX_TOKEN is empty — .env did not load. Every query will 401.')
        return 1
    n_open = sum(1 for k in market.views if market._exchange_open(k))
    if n_open < len(market.views) and not args.all:
        print(f'\n  WARNING: only {n_open} of {len(market.views)} instruments are '
              f'inside their exchange hours right now\n'
              f'  ({datetime.now(IST):%H:%M} IST). That is NOT the load the engine '
              f'carries at midday.\n  Re-run with --all to query them anyway, or '
              f'run this during market hours.\n')

    hdr = (f"  {'cycle':<7}{'stale':>10}{'fetched':>10}{'query':>10}"
           f"{'append':>10}{'total':>10}{'held':>12}")
    print(hdr)
    print('  ' + '-' * (len(hdr) - 2))

    for i in range(CYCLES):
        keys = ([k for k in market.views]
                if args.all
                else [k for k in market.views if market._exchange_open(k)])
        if not keys:
            print('  no instrument is inside its exchange hours — nothing to measure')
            return 1

        since = {k: market.views[k].last_ts for k in keys}
        stale = sum(1 for v in since.values() if v is None)

        t0 = time.perf_counter()
        got = reader.fetch_many(keys, since)
        t_query = time.perf_counter() - t0

        fetched = sum(len(d) for d in got.values())

        t0 = time.perf_counter()
        for k in keys:
            market.views[k]._append(got.get(k, pd.DataFrame()))
        t_append = time.perf_counter() - t0

        held = sum(len(v.ticks) for v in market.views.values())
        print(f'  {i:<7}{stale:>4}/{len(keys):<5}{fetched:>10,}'
              f'{t_query * 1000:>9.0f}ms{t_append * 1000:>9.0f}ms'
              f'{(t_query + t_append) * 1000:>9.0f}ms{held:>12,}')

        if i < CYCLES - 1:
            time.sleep(GAP_SECS)

    print('\nHow to read this:')
    print('  stale stuck at N/99 on warm cycles -> watermarks are NOT advancing;')
    print('    every cycle re-scans LOOKBACK_MINUTES for those N. That is the bug.')
    print('  query large, stale ~0                -> the query itself is slow; look at')
    print('    INFLUX_FILTER_BY and the age-band split in fetch_many().')
    print('  append large                         -> _trim() rebuilds each buffer every')
    print('    cycle; it trims by LOOKBACK_MINUTES, not by the ~2000 ticks the strategy')
    print('    actually needs (ANGLE_WINDOW).')
    print('  everything small                     -> the cost is NOT in refresh(); time')
    print('    engine.run_cycle() instead, since t_read wraps more than the query.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
