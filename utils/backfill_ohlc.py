#!/usr/bin/env python3
"""Backfill the 15:30-15:40 window that the writer discarded from 2026-08-03.

    python utils/backfill_ohlc.py --from 20260803 --to 20260818 --report
    python utils/backfill_ohlc.py --from 20260817 --to 20260818          # write

WHAT HAPPENED
-------------
SEBI's Closing Auction Session took effect 2026-08-03 and the equity derivatives
session was extended to 15:40. viz_hedge/src/services/influx_writer.py had
NSE_BSE_CLOSE hard-coded to 15:30 and DROPS NSE/BSE points after it, so roughly the
last ten minutes of every session since then were never stored. The feeder kept
running (SESSION_END is 23:35 for MCX) and nothing logged an error.

That window is not filler. A dry-run poll on 2026-08-18 pulled the 15:38 candle with
119,540 traded on the ATM strike in that single minute, and the one effect that has
replicated across this project is that large moves cluster at the SESSION EDGES.

RETENTION IS THE DEADLINE
-------------------------
Upstox serves 1-minute candles for approximately the FINAL MONTH up to the end date.
2026-08-03 is inside that window as of 2026-08-18, and will fall out of it in early
September. After that these sessions are permanently incomplete. Run this first and
argue about the schema later.

EXPIRED CONTRACTS ARE THE HARD PART — READ BEFORE TRUSTING THE OUTPUT
--------------------------------------------------------------------
Sessions from 3-13 August traded the 06 AUG and 13 AUG expiries, which no longer
exist in the instrument master, so their keys cannot be resolved from today's symbol
map and the normal historical endpoint may refuse them. Those dates need the
Expired Instruments API, which this script does NOT implement:

    https://upstox.com/developer/api-documentation/expired-instruments

The script reports every measurement it could not resolve rather than skipping it
quietly. Expect 17-18 August (20 AUG expiry, still live) to backfill cleanly and the
earlier dates to need that second path. A partial backfill that LOOKS complete is
worse than none, which is why --report exists and runs first.

WHAT GETS WRITTEN, AND THE HONEST CAVEAT
----------------------------------------
Candles are written to the SAME bucket and measurement as the tick data, tagged
`source=backfill`, carrying open/high/low/close/volume/oi AND `ltp` set to the
candle close.

`ltp` is written deliberately: every existing analysis script filters on
`_field == "ltp"`, so without it the backfilled window would be invisible to all of
them and the exercise would be pointless. The cost is that within a session `ltp` is
now tick-resolution until 15:30 and 1-minute closes afterwards. For P&L and
level-based work that is fine. For MICROSTRUCTURE work — anything reading tick
spacing, autocorrelation, or per-print behaviour — it is not, and you must exclude
it:

    |> filter(fn: (r) => r.source != "backfill")

Nothing is written where tick data already exists: each measurement is backfilled
only past its own last stored timestamp for that date.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                             # noqa: E402,F401

from src.config import settings                              # noqa: E402
from poll_ohlc import load_token                             # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
HIST_URL = 'https://api.upstox.com/v3/historical-candle'
SEGMENTS = ('BSE_FO', 'NSE_FO')
RATE_SLEEP = 0.08          # 25 req/s allowed; this is ~12/s


def day_bounds(date_str: str):
    d = datetime.strptime(date_str, '%Y%m%d').date()
    return d, d.strftime('%Y-%m-%d')


def existing_coverage(date_str: str) -> dict:
    """measurement -> last stored tick timestamp (IST) for that date."""
    from influxdb_client import InfluxDBClient
    d, _ = day_bounds(date_str)
    segs = ' or '.join(f'r.segment == "{s}"' for s in SEGMENTS)
    q = (f'from(bucket: "{settings.INFLUX_BUCKET}")\n'
         f'  |> range(start: {d}T00:00:00+05:30, '
         f'stop: {d + timedelta(days=1)}T00:00:00+05:30)\n'
         f'  |> filter(fn: (r) => {segs})\n'
         f'  |> filter(fn: (r) => r._field == "ltp")\n'
         f'  |> group(columns: ["_measurement"])\n'
         f'  |> last()\n'
         f'  |> keep(columns: ["_measurement", "_time"])')
    with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                        org=settings.INFLUX_ORG, timeout=180_000) as c:
        tables = c.query_api().query(q)
    out = {}
    for t in tables:
        for r in t.records:
            out[r.values.get('_measurement')] = r.get_time().astimezone(IST)
    return out


def master_map(master_dir: Path) -> dict:
    """measurement -> instrument_key from the FULL instrument master.

    NOT from _load_symbol_map(): that resolves only the keys currently SUBSCRIBED,
    so any strike traded on an earlier day but absent from today's chain looks
    "expired" when it is simply not in today's subscription. On 2026-08-17 that
    misreported 32 of 44 live 20-AUG contracts as unrecoverable and backfilled only
    12 of them.

    The master holds every live contract, so what remains unresolved after this is
    genuinely expired and genuinely needs the Expired Instruments API.
    """
    out = {}
    for p in sorted(Path(master_dir).glob('*.json')):
        try:
            rows = json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            print(f'  could not read {p.name}: {e}')
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            k, s = row.get('instrument_key'), row.get('trading_symbol')
            if k and s:
                out[f"{str(k).split('|')[0].split('_')[0]}_{s}"] = k
    return out


def find_master_dir(explicit: str | None) -> Path | None:
    cands = [Path(explicit)] if explicit else [
        REPO_ROOT.parent / 'viz_hedge' / 'data',
        Path.home() / 'viz_hedge' / 'data',
        REPO_ROOT / 'data',
    ]
    for c in cands:
        if c.is_dir() and any(c.glob('*.json')):
            return c
    return None


def fetch_candles(key: str, date_str: str, token: str, session=None) -> list:
    """1-minute candles for one instrument on one date -> [(start_dt, o,h,l,c,v,oi)].

    Two different endpoints, and using the wrong one returns an EMPTY list rather
    than an error:
      past dates  /v3/historical-candle/{key}/minutes/1/{to}/{from}
      TODAY       /v3/historical-candle/intraday/{key}/minutes/1
    The historical endpoint does not serve the current session. That is why the
    first run backfilled 0 points for 2026-08-18 while reporting 22 resolved
    instruments -- it looked like a permissions or expiry problem and was neither.
    """
    d, iso = day_bounds(date_str)
    if d == datetime.now(IST).date():
        url = f'{HIST_URL}/intraday/{quote(key, safe="")}/minutes/1'
    else:
        url = f'{HIST_URL}/{quote(key, safe="")}/minutes/1/{iso}/{iso}'
    http = session or requests
    r = http.get(url, headers={'Accept': 'application/json',
                               'Authorization': f'Bearer {token}'}, timeout=30)
    if r.status_code == 401:
        raise SystemExit('Upstox returned 401 — access token expired or missing.')
    if r.status_code >= 400:
        # 400/404 here is the expired-contract case far more often than a bug
        raise RuntimeError(f'HTTP {r.status_code}: {str(r.text)[:180]}')
    body = r.json()
    if body.get('status') != 'success':
        raise RuntimeError(f'status={body.get("status")}: {str(body)[:180]}')
    rows = []
    for c in ((body.get('data') or {}).get('candles') or []):
        if len(c) < 6:
            continue
        ts = datetime.fromisoformat(str(c[0])).astimezone(IST)
        rows.append((ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                     float(c[5] or 0), float(c[6]) if len(c) > 6 and c[6] is not None
                     else None))
    rows.sort(key=lambda x: x[0])
    return rows


def write_candles(rows: list, measurement: str, key: str, bucket: str) -> int:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    pts = []
    for (start, o, h, low, cl, vol, oi) in rows:
        # right edge, matching poll_ohlc and the label='right' convention that fixed
        # the fake 84% hit rate. ts_start keeps the source value.
        stamp = start + timedelta(minutes=1)
        p = (Point(measurement)
             .tag('instrument_key', key)
             .tag('segment', str(key).split('|')[0])
             .tag('source', 'backfill')
             .field('open', o).field('high', h).field('low', low)
             .field('close', cl).field('volume', vol)
             .field('ltp', cl)               # see module docstring
             .field('ts_start', float(start.timestamp() * 1000))
             .time(stamp, WritePrecision.NS))
        if oi is not None:
            p = p.field('oi', oi)
        pts.append(p)
    if not pts:
        return 0
    with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                        org=settings.INFLUX_ORG, timeout=120_000) as c:
        c.write_api(write_options=SYNCHRONOUS).write(
            bucket=bucket, org=settings.INFLUX_ORG, record=pts)
    return len(pts)


def dates_between(a: str, b: str) -> list:
    d0 = datetime.strptime(a, '%Y%m%d').date()
    d1 = datetime.strptime(b, '%Y%m%d').date()
    out, d = [], d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.strftime('%Y%m%d'))
        d += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--from', dest='d_from', required=True, help='YYYYMMDD')
    ap.add_argument('--to', dest='d_to', required=True, help='YYYYMMDD')
    ap.add_argument('--bucket', default=None, help='default: INFLUX_BUCKET')
    ap.add_argument('--report', action='store_true',
                    help='show the hole per day and resolve keys; write nothing')
    ap.add_argument('--cutoff', default='15:29',
                    help='flag any day whose last tick is at/before this (HH:MM)')
    ap.add_argument('--master-dir', default=None,
                    help='dir holding the instrument master JSONs '
                         '(default: ../viz_hedge/data or ~/viz_hedge/data)')
    a = ap.parse_args()

    bucket = a.bucket or settings.INFLUX_BUCKET
    dates = dates_between(a.d_from, a.d_to)
    if not dates:
        print('no weekdays in range')
        return 1

    mdir = find_master_dir(a.master_dir)
    if not mdir:
        print('Could not find the instrument master (viz_hedge/data/*.json). '
              'Pass --master-dir. Without it almost everything reports as expired.')
        return 1
    by_meas = master_map(mdir)
    print(f'instrument master: {mdir}  ({len(by_meas):,} contracts)')

    ch, cm = a.cutoff.split(':')
    cutoff = int(ch) * 60 + int(cm)
    token = None if a.report else load_token()
    sess = requests.Session()

    grand_written, unresolved_all, failed_all = 0, set(), []
    for date_str in dates:
        cov = existing_coverage(date_str)
        if not cov:
            print(f'\n{date_str}: no NSE/BSE tick data at all — holiday, or the '
                  f'feeder did not run')
            continue
        truncated = {m: t for m, t in cov.items()
                     if t.hour * 60 + t.minute <= cutoff}
        latest = max(cov.values())
        print(f'\n{date_str}: {len(cov)} measurements, last tick {latest:%H:%M:%S}, '
              f'{len(truncated)} truncated at/before {a.cutoff}')
        if not truncated:
            print('  nothing missing — skipping')
            continue

        resolved = {m: by_meas[m] for m in truncated if m in by_meas}
        unresolved = sorted(set(truncated) - set(resolved))
        if unresolved:
            unresolved_all.update(unresolved)
            print(f'  {len(unresolved)} measurement(s) absent from the FULL '
                  f'instrument master — these really are expired contracts:')
            for m in unresolved[:4]:
                print(f'      {m}')
            if len(unresolved) > 4:
                print(f'      ... and {len(unresolved) - 4} more')
            print('  these need the Expired Instruments API; this script cannot '
                  'reach them')
        if a.report or not resolved:
            continue

        wrote = 0
        for meas, key in sorted(resolved.items()):
            try:
                rows = fetch_candles(key, date_str, token, sess)
            except SystemExit:
                raise
            except Exception as e:
                failed_all.append(f'{date_str} {meas}: {str(e)[:110]}')
                continue
            finally:
                time.sleep(RATE_SLEEP)
            # only past what is already stored — never duplicate real ticks
            have = truncated[meas]
            new = [r for r in rows if r[0] + timedelta(minutes=1) > have]
            if not new:
                continue
            wrote += write_candles(new, meas, key, bucket)
        grand_written += wrote
        print(f'  wrote {wrote} candle points across {len(resolved)} instruments')

    print(f'\n{"=" * 70}')
    print(f'total points written: {grand_written}')
    if unresolved_all:
        print(f'\n{len(unresolved_all)} measurement(s) unresolved across all dates.')
        print('These are expired contracts. Until they are fetched via the Expired')
        print('Instruments API, any study spanning early August is still missing its')
        print('closing window — do NOT treat the backfill as complete.')
    if failed_all:
        print(f'\n{len(failed_all)} fetch failure(s):')
        for f in failed_all[:10]:
            print(f'  {f}')
    if grand_written:
        print('\nBackfilled points are tagged source="backfill" and carry 1-minute')
        print('resolution, not ticks. Exclude them from microstructure work with:')
        print('    |> filter(fn: (r) => r.source != "backfill")')
        print('\nVerify:')
        print(f'  influx query --org {settings.INFLUX_ORG} \'')
        print(f'  from(bucket: "{bucket}") |> range(start: -20d)')
        print('    |> filter(fn: (r) => r._field == "ltp" and r.segment == "BSE_FO")')
        print('    |> group(columns: ["_measurement"]) |> last()\'')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
