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
import os
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

# PER-EXCHANGE HOURS, NOT ONE WINDOW.
# MCX trades 09:00-23:30; NSE/BSE derivatives 09:15-15:40 (15:40 since the Closing
# Auction Session took effect 2026-08-03 — see influx_writer.NSE_BSE_CLOSE).
# A single 09:15-15:40 window would silently drop ~8 hours of commodity candles
# every day while appearing to work, which is precisely the shape of the
# NSE_BSE_CLOSE bug that cost 13 sessions of closing data. Open at 09:00 for MCX.
SESSION_OPEN = os.getenv('POLL_SESSION_OPEN', '09:00')
EXCHANGE_CLOSE_RAW = os.getenv('POLL_EXCHANGE_CLOSE',
                               'NSE:15:40,BSE:15:40,MCX:23:30')
VIX_KEY = 'NSE_INDEX|India VIX'


def _hhmm(s: str, default: dt_time) -> dt_time:
    try:
        h, m = str(s).strip().split(':')
        return dt_time(int(h), int(m))
    except Exception:
        return default


MARKET_OPEN = _hhmm(SESSION_OPEN, dt_time(9, 0))
EXCHANGE_CLOSE = {}
for _p in EXCHANGE_CLOSE_RAW.split(','):
    _b = _p.split(':')
    if len(_b) == 3:
        EXCHANGE_CLOSE[_b[0].strip().upper()] = _hhmm(f'{_b[1]}:{_b[2]}',
                                                      dt_time(15, 40))


def exchange_of(key: str) -> str:
    """BSE_FO|123 -> BSE, NSE_INDEX|India VIX -> NSE, MCX_FO|9 -> MCX."""
    return str(key).split('|')[0].split('_')[0].upper()


def key_open(key: str, when=None) -> bool:
    n = when or _now()
    if n.weekday() >= 5:
        return False
    close = EXCHANGE_CLOSE.get(exchange_of(key), dt_time(15, 40))
    return MARKET_OPEN <= n.time() <= close


def any_open(keys, when=None) -> bool:
    return any(key_open(k, when) for k in keys)

POLL_OFFSET_SECS = 5      # poll at :05 past the minute so prev_ohlc is settled
MAX_CONSECUTIVE_FAIL = 5  # then exit non-zero and let OnFailure alert

# A ONE-MINUTE GAP ON A THIN CONTRACT IS NOT NEWS — it means nobody traded that
# minute, the API did not advance prev_ohlc.ts, and dedup correctly wrote nothing.
# Measured on 2026-08-24: CRUDEOILM 8200 and NATURALGAS 260 options produced ~15
# such lines in 20 minutes while SILVERM (658-3,700 lots/min) produced none.
# Logging each one individually buries the gaps that DO matter — a 5-minute hole
# from an expired token looks identical in a stream of hundreds. So single-minute
# gaps are counted and summarised; anything longer is printed the moment it happens.
GAP_WARN_MINUTES = int(os.getenv('OHLC_GAP_WARN', '2'))
GAP_SUMMARY_EVERY = int(os.getenv('OHLC_GAP_SUMMARY_EVERY', '60'))   # polls


def _now() -> datetime:
    return datetime.now(IST)


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
        self.gaps: list[str] = []          # only gaps >= GAP_WARN_MINUTES
        self.missed: dict[str, int] = {}   # every missing minute, for the summary

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
                    name = self.name_of(key)
                    self.missed[name] = self.missed.get(name, 0) + missed
                    # only surface the ones that are not just a quiet minute
                    if missed >= GAP_WARN_MINUTES:
                        self.gaps.append(
                            f'{name}: {missed} minute(s) missing before '
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

    def due_keys(self, ignore_hours: bool = False) -> list:
        """Only instruments whose own exchange is still trading.

        Asking Upstox for an NSE index at 20:00 returns its stale 15:29 candle
        forever; dedup drops it, but it wastes the request and — worse — makes the
        summary line show a candle time that has nothing to do with the MCX rows
        beside it."""
        return self.keys if ignore_hours else [k for k in self.keys if key_open(k)]

    def poll_once(self, ignore_hours: bool = False) -> list:
        keys = self.due_keys(ignore_hours)
        rows = []
        for i in range(0, len(keys), self.chunk):
            batch = keys[i:i + self.chunk]
            rows.extend(self.parse(self._get(OHLC_URL, batch)))
            if i + self.chunk < len(keys):
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


def env_list(path: Path, key: str) -> list:
    """Comma-separated value out of a .env, without sourcing it.

    SUBSCRIBE_INSTRUMENTS contains '|' in every entry, so `source` would try to
    pipe them as shell commands."""
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            if line.startswith(f'{key}='):
                v = line.split('=', 1)[1].split('#')[0].strip().strip('"\'')
                return [x.strip() for x in v.split(',') if x.strip()]
    except OSError:
        pass
    return []


def _has_master(p: Path):
    return p if p.is_dir() and any(p.glob('*.json')) else None


def find_master_dir(explicit=None):
    """Where viz_hedge keeps NSE.json / BSE.json / MCX.json.

    Explicit config is AUTHORITATIVE — if --master-dir or VIZHEDGE_DIR is given and
    holds no master, return None rather than quietly searching elsewhere. Falling
    back would resolve symbols against a different (possibly stale) instrument set
    and write plausible-looking measurement names for the wrong contracts. Only when
    nothing is configured do we go looking.
    """
    import os as _os
    if explicit:
        return _has_master(Path(explicit))
    env = _os.getenv('VIZHEDGE_DIR')
    if env:
        return _has_master(Path(env) / 'data')
    for c in (Path.home() / 'viz_hedge' / 'data',
              REPO_ROOT.parent / 'viz_hedge' / 'data',
              REPO_ROOT / 'data'):
        if _has_master(c):
            return c
    return None


def symbol_map_for(keys, master_dir) -> dict:
    """instrument_key -> trading_symbol, for EVERY key we poll.

    NOT src.main._load_symbol_map(): that resolves only ANALYZE_INSTRUMENTS, the
    SENSEX chain the engine scores. Polling the whole feed with it left NSE keys
    unresolved, so this bucket would have written `NSE_FO_61734` while the feeder
    writes `NSE_NIFTY 24000 CE 25 AUG 26` for the same contract — two names for one
    instrument across two buckets, which silently breaks any join between them.
    Reading the master directly covers all of them.
    """
    import json
    want, out = set(keys), {}
    if not master_dir:
        return out
    for p in sorted(Path(master_dir).glob('*.json')):
        try:
            rows = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            k = r.get('instrument_key')
            if k in want and r.get('trading_symbol'):
                out[k] = r['trading_symbol']
    return out


def load_keys(args) -> list:
    """Everything the FEEDER subscribes to, not just what vizsignals analyses.

    ANALYZE_INSTRUMENTS is the SENSEX chain the engine scores. SUBSCRIBE_INSTRUMENTS
    is the full feed — NSE, BSE and MCX. For an OHLC archive we want the latter, so
    viz_hedge's .env is the primary source and vizsignals' settings is the fallback.
    """
    import os as _os
    if args.instruments:
        keys = [k.strip() for k in args.instruments.split(',') if k.strip()]
    else:
        hedge = Path(_os.getenv('VIZHEDGE_DIR', str(Path.home() / 'viz_hedge')))
        keys = env_list(hedge / '.env', 'SUBSCRIBE_INSTRUMENTS')
        src = f'{hedge}/.env SUBSCRIBE_INSTRUMENTS'
        if not keys:
            keys = list(getattr(settings, 'ANALYZE_INSTRUMENTS', []) or [])
            src = 'settings.ANALYZE_INSTRUMENTS'
        print(f'  instruments from {src}')
    if args.vix and VIX_KEY not in keys:
        keys.append(VIX_KEY)
    if not keys:
        raise SystemExit('No instruments. Pass --instruments, or set '
                         'SUBSCRIBE_INSTRUMENTS in viz_hedge/.env.')
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--instruments', help='comma-separated keys (default: '
                                          'ANALYZE_INSTRUMENTS)')
    # ohlcv_data, matching the tick_data naming convention
    ap.add_argument('--bucket', default=os.getenv('OHLCV_BUCKET', 'ohlcv_data'))
    ap.add_argument('--chunk', type=int, default=100)
    ap.add_argument('--vix', action='store_true', default=True,
                    help='include NSE_INDEX|India VIX (default on)')
    ap.add_argument('--no-vix', dest='vix', action='store_false')
    ap.add_argument('--once', action='store_true', help='one poll, then exit')
    ap.add_argument('--dry-run', action='store_true', help='print, do not write')
    ap.add_argument('--ignore-hours', action='store_true')
    ap.add_argument('--master-dir', default=None,
                    help='instrument master dir (default: viz_hedge/data)')
    a = ap.parse_args()

    keys = load_keys(a)
    mdir = find_master_dir(a.master_dir)
    smap = symbol_map_for(keys, mdir)
    miss = [k for k in keys if k not in smap and not k.startswith('NSE_INDEX')]
    print(f'  symbol map: {len(smap)}/{len(keys)} resolved from '
          f'{mdir or "NO MASTER FOUND"}')
    if miss:
        # Loud, because an unresolved key writes to a DIFFERENT measurement name
        # than the feeder uses for the same contract.
        print(f'  {len(miss)} unresolved -> will write as {{segment}}_{{token}}, '
              f'which will NOT match tick_data. e.g. {miss[:2]}')

    poller = OhlcPoller(keys, load_token(), smap, chunk=a.chunk)
    by_exch = {}
    for k in keys:
        by_exch[exchange_of(k)] = by_exch.get(exchange_of(k), 0) + 1
    hours = '  '.join(f'{e}->{EXCHANGE_CLOSE.get(e, dt_time(15, 40)):%H:%M}'
                      for e in sorted(by_exch))
    print(f'poll_ohlc: {len(keys)} instruments '
          f'({", ".join(f"{e} {n}" for e, n in sorted(by_exch.items()))}), '
          f'bucket={a.bucket}, {"DRY RUN" if a.dry_run else "writing"}')
    print(f'  open {MARKET_OPEN:%H:%M}, closes  {hours}')

    last_close = max(EXCHANGE_CLOSE.get(e, dt_time(15, 40)) for e in by_exch) \
        if by_exch else dt_time(15, 40)

    fails = polls = 0
    while True:
        if not (a.ignore_hours or any_open(keys)):
            if a.once:
                print('  no exchange open — use --ignore-hours to poll anyway')
                return 0
            n = _now()
            if n.time() > last_close or n.weekday() >= 5:
                print(f'  all exchanges closed at {n:%H:%M:%S} IST — stopping cleanly')
                return 0
            time.sleep(20)
            continue

        try:
            rows = poller.poll_once(a.ignore_hours)
            if not rows:
                fails += 1
                print(f'  {_now():%H:%M:%S}  NO CANDLES ({fails}/{MAX_CONSECUTIVE_FAIL})',
                      file=sys.stderr)
            else:
                fails = 0
                n = 0 if a.dry_run else write_influx(rows, a.bucket)
                # RANGE, not rows[0]. Mixed segments have different candle times —
                # an NSE index after 15:30 returns its stale 15:29 bar while MCX is
                # current, and printing only the first row asserted one time for all
                # of them. A single stamp here is only correct when they agree.
                stamps = sorted(r['ts_start'] for r in rows)
                lo = datetime.fromtimestamp(stamps[0] / 1000, IST)
                hi = datetime.fromtimestamp(stamps[-1] / 1000, IST)
                span = f'{lo:%H:%M}' if lo == hi else f'{lo:%H:%M}-{hi:%H:%M}'
                print(f'  {_now():%H:%M:%S}  candle {span}  '
                      f'{len(rows)} instruments'
                      + (f'  wrote {n}' if not a.dry_run else '  (dry run)'))
                if a.dry_run:
                    for r in rows[:6]:
                        ts = datetime.fromtimestamp(r['ts_start'] / 1000, IST)
                        print(f'      {ts:%H:%M} {r["symbol"][:26]:<26} '
                              f'O {r["open"]:>9.2f} H {r["high"]:>9.2f} '
                              f'L {r["low"]:>9.2f} C {r["close"]:>9.2f} '
                              f'V {r["volume"]:>9,.0f}')
                while poller.gaps:
                    print(f'  GAP: {poller.gaps.pop(0)}', file=sys.stderr)
                polls += 1
                if polls % GAP_SUMMARY_EVERY == 0 and poller.missed:
                    top = sorted(poller.missed.items(), key=lambda x: -x[1])[:5]
                    print(f'  quiet minutes over the last {GAP_SUMMARY_EVERY} polls: '
                          + ', '.join(f'{n} {c}' for n, c in top))
                    poller.missed.clear()
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
