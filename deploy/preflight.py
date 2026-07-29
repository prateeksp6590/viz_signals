"""
viz_signals readiness gate. Read-only: no orders, no writes, nothing scheduled.

    cd ~/viz_signals && .venv/bin/python deploy/preflight.py [--date YYYY-MM-DD]

It replays a PAST session out of the tick_data bucket through the real
InfluxReader -> MarketData -> SlopeAngleStrategy chain, which is the only way to
exercise the batched Flux query (measurement names contain spaces) and the
strategy wiring before market open.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import warnings
from dotenv import load_dotenv
load_dotenv()
try:                                   # metadata queries legitimately have no pivot
    from influxdb_client.client.warnings import MissingPivotFunction
    warnings.simplefilter('ignore', MissingPivotFunction)
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))
P = F = 0


def ok(m):
    global P; P += 1; print(f'  \033[32mPASS\033[0m  {m}')


def no(m):
    global F; F += 1; print(f'  \033[31mFAIL\033[0m  {m}')


def warn(m):
    print(f'  \033[33mWARN\033[0m  {m}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='past session to replay, YYYY-MM-DD (default: last weekday)')
    ap.add_argument('--max-instruments', type=int, default=6)
    args = ap.parse_args()

    print(f'=== viz_signals preflight  {datetime.now(IST):%F %T %Z} ===')

    # ── 1. config ─────────────────────────────────────────────────────────────
    print('\n-- 1. config')
    from src.config import settings
    errs = settings.validate()
    if errs:
        for e in errs:
            no(e)
    else:
        ok('settings.validate() clean')
    print(f'        bucket={settings.INFLUX_BUCKET}  mode={settings.ORDER_MODE}  '
          f'poll={settings.POLL_INTERVAL_SECS}s  lookback={settings.LOOKBACK_MINUTES}m')
    print(f'        angle: {settings.ANGLE_PRICE_MODE}/{settings.ANGLE_THRESH_MODE} '
          f'q={settings.ANGLE_Q} w={settings.ANGLE_WINDOW} n1={settings.ANGLE_N1} '
          f'n2={settings.ANGLE_N2} long_only={settings.ANGLE_LONG_ONLY}')
    if settings.ORDER_MODE != 'signals_only':
        warn(f"ORDER_MODE={settings.ORDER_MODE} — use signals_only until the forward test has run")
    else:
        ok('ORDER_MODE=signals_only (no orders will be placed)')
    if not settings.ANALYZE_INSTRUMENTS:
        no('ANALYZE_INSTRUMENTS empty — copy it from the feeder (see README)')
        return 1
    ok(f'{len(settings.ANALYZE_INSTRUMENTS)} instrument key(s) configured')

    # ── 2. symbol map ─────────────────────────────────────────────────────────
    print('\n-- 2. symbol map (measurement names)')
    key_set = set(settings.ANALYZE_INSTRUMENTS)
    smap, ddir = {}, settings.NSE_JSON_PATH.parent
    for ex in ('NSE', 'BSE', 'MCX'):
        p = ddir / f'{ex}.json'
        if not p.exists():
            warn(f'{p} missing')
            continue
        try:
            for inst in json.loads(p.read_text()):
                if inst.get('instrument_key') in key_set:
                    smap[inst['instrument_key']] = inst['trading_symbol']
        except Exception as e:
            no(f'could not parse {p}: {e}')
    if len(smap) == len(key_set):
        ok(f'all {len(smap)} keys resolved to trading symbols')
    else:
        no(f'only {len(smap)}/{len(key_set)} keys resolved — measurement names would '
           f'fall back to raw keys and never match the feeder')

    # ── 3. live InfluxDB read (the untested path) ─────────────────────────────
    print('\n-- 3. InfluxDB read path')
    from src.services.influx_reader import InfluxReader
    reader = InfluxReader(smap)
    try:
        present = reader.list_measurements(days=4)
        ok(f'connected; {len(present)} measurement(s) in "{settings.INFLUX_BUCKET}" (last 4d)')
    except Exception as e:
        no(f'connect/list failed: {e}')
        return 1

    want = {reader.measurement_name(k): k for k in settings.ANALYZE_INSTRUMENTS}
    hit = [m for m in want if m in present]
    if hit:
        ok(f'{len(hit)}/{len(want)} configured measurements exist in the bucket')
    else:
        no('none of the configured measurements exist — is ANALYZE_INSTRUMENTS from '
           'the same day the feeder ran?')
        print('        bucket sample:', present[:4])

    # replay window: the chosen date, else the most recent weekday
    if args.date:
        day = datetime.strptime(args.date, '%Y-%m-%d').replace(tzinfo=IST)
    else:
        day = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        day -= timedelta(days=1)
        while day.weekday() > 4:
            day -= timedelta(days=1)
    start = day.replace(hour=9, minute=15)
    stop = day.replace(hour=15, minute=30)
    print(f'        replaying {start:%F} 09:15-15:30 IST')

    probe = [want[m] for m in hit[:args.max_instruments]] or settings.ANALYZE_INSTRUMENTS[:2]
    try:
        one = reader.fetch_range(probe[0], start, stop)
        if len(one):
            ok(f'fetch_range: {len(one):,} rows for {reader.measurement_name(probe[0])}; '
               f'fields={sorted(c for c in one.columns)[:8]}')
            for f in ('vtt', 'ltq'):
                (ok if f in one.columns else warn)(
                    f'field {f} {"present" if f in one.columns else "absent (pre-fix data)"}')
        else:
            warn('fetch_range returned 0 rows — no data for that day?')
    except Exception as e:
        no(f'fetch_range failed: {e}')

    # The batched query is the production path, so test the two loads production
    # actually issues: a small incremental poll, and the worst case cold start.
    import time as _t
    inc_since = stop - timedelta(minutes=2)
    try:
        t0 = _t.time()
        got = reader.fetch_many(probe, {k: inc_since for k in probe})
        dt = _t.time() - t0
        tot = sum(len(v) for v in got.values())
        if got:
            ok(f'fetch_many incremental (2 min, {len(probe)} instruments): '
               f'{len(got)} returned, {tot:,} rows in {dt:.1f}s')
        else:
            no('fetch_many returned nothing for a window where fetch_range found data '
               '- the batched contains()/groupby path is broken (this IS production)')
    except Exception as e:
        no(f'fetch_many raised: {e}')

    # cold start: every view empty -> one LOOKBACK_MINUTES window over ALL instruments.
    # This is what runs if the engine restarts mid-session, and it is the query that
    # times out if the instrument set is not chunked.
    cold_since = stop - timedelta(minutes=settings.LOOKBACK_MINUTES)
    allk = settings.ANALYZE_INSTRUMENTS
    try:
        t0 = _t.time()
        got = reader.fetch_many(allk, {k: cold_since for k in allk})
        dt = _t.time() - t0
        tot = sum(len(v) for v in got.values())
        nq = -(-len(set(reader.measurement_name(k) for k in allk)) // settings.INFLUX_QUERY_CHUNK)
        if not got:
            no(f'cold start returned nothing ({len(allk)} instruments, '
               f'{settings.LOOKBACK_MINUTES}m) - check the log above for a timeout')
        elif dt > 30:
            warn(f'cold start SLOW: {tot:,} rows in {dt:.1f}s over {nq} chunk(s). '
                 f'Lower INFLUX_QUERY_CHUNK or LOOKBACK_MINUTES.')
        else:
            ok(f'cold start: {len(got)}/{len(allk)} instruments, {tot:,} rows in '
               f'{dt:.1f}s over {nq} chunk(s) of {settings.INFLUX_QUERY_CHUNK}')
        print(f'        fields pulled: {settings.INFLUX_FIELDS or "ALL"}  '
              f'(poll interval is {settings.POLL_INTERVAL_SECS}s - a cycle must finish '
              f'well inside it)')
    except Exception as e:
        no(f'cold start raised: {e}')

    # ── 4. strategy over that real data ──────────────────────────────────────
    print('\n-- 4. strategy on real ticks')
    from src.services.market_view import InstrumentView
    from src.strategies.slope_angle import SlopeAngleStrategy
    strat = SlopeAngleStrategy()
    print(f'        warmup needed: {strat.warmup_ticks:,} ticks')
    fired = short = 0
    for k in probe:
        df = reader.fetch_range(k, start, stop)
        if df.empty:
            continue
        v = InstrumentView(k, smap.get(k, k))
        v.ticks = df
        if len(df) < strat.warmup_ticks:
            short += 1
            continue
        try:
            sigs = strat.generate_signals(v)
        except Exception as e:
            no(f'strategy raised on {v.symbol}: {e}')
            continue
        if sigs:
            fired += 1
            s = sigs[0]
            print(f'        {v.symbol[:34]:34} {s.action.value:11} '
                  f"angle {s.meta['angle_deg']:.2f} >= {s.meta['threshold_deg']:.2f}")
    ok(f'strategy ran on {len(probe)} instrument(s) without raising')
    if short:
        warn(f'{short} instrument(s) had fewer than {strat.warmup_ticks} ticks')
    print(f'        (a signal at the final tick of the day is a snapshot, not a count — '
          f'{fired} instrument(s) would have fired there)')

    # ── 5. footprint ─────────────────────────────────────────────────────────
    print('\n-- 5. footprint')
    try:
        import shutil
        avail = int(os.popen("free -m | awk '/^Mem:/{print $7}'").read() or 0)
        # rows = instruments x minutes x 60s x ticks/sec; ~12 float64 cols; pandas
        # overhead ~3x the raw buffer.
        n = len(settings.ANALYZE_INSTRUMENTS)
        rows = n * settings.LOOKBACK_MINUTES * 60 * 1.3
        est = int(rows * 12 * 8 * 3 / 1_000_000)
        print(f'        rough need ~{est} MB ({rows/1e6:.2f}M rows) for {n} instruments '
              f'x {settings.LOOKBACK_MINUTES}m; {avail} MB available')
        (ok if avail > est * 3 else warn)(
            f'memory headroom {"adequate" if avail > est*3 else "tight — cut LOOKBACK_MINUTES or trim instruments"}')
    except Exception:
        warn('could not read memory')

    reader.close()
    print('\n' + '=' * 52)
    print(f'  {P} passed, {F} failed')
    print('  READY' if F == 0 else '  NOT READY')
    print('=' * 52)
    return 1 if F else 0


if __name__ == '__main__':
    sys.exit(main())
