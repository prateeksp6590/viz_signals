"""
MCX via REST polling, not the websocket.

Commodity options tick 4-6k times a session against 34k for a NIFTY leg, so a
streaming connection is wasted on them. This polls Upstox market-quote every
POLL_MINS minutes, writes to the SAME InfluxDB bucket and schema the feeder uses, and
evaluates signals on 5-minute bars.

IMPORTANT: this is a DIFFERENT strategy from the NSE/BSE one, not the same strategy
slowed down. n1/n2 are counts of 2-minute samples here, so MCX_N1=5/MCX_N2=8 means a
10- and 16-minute geometry. The NSE calibration (q=0.95 on tick data) carries no
evidence for this timescale; it must be validated separately on its own journal.

    python -m mcx.poller
"""

import json
import os
import signal as _signal
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from influxdb_client import Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from src.config import settings
from src.services.notifier import Notifier
from src.strategies.angle_math import (adaptive_threshold_latest, angle_series,
                                       is_upward_bend)
from src.models import Signal, SignalAction
from src.utils.logger import logger

IST = timezone(timedelta(hours=5, minutes=30))
QUOTE_URL = 'https://api.upstox.com/v2/market-quote/quotes'
ROOT = Path(__file__).resolve().parent.parent


def _env(k, d=None):
    import re
    v = os.getenv(k)
    return re.sub(r'\s+#.*$', '', v).strip() if v else d


START = _env('MCX_START', '09:05')
STOP = _env('MCX_STOP', '23:35')
POLL_MINS = float(_env('MCX_POLL_MINS', '2'))
BAR_MINS = float(_env('MCX_BAR_MINS', '5'))
UNDERLYINGS = [x.strip().upper() for x in
               _env('MCX_UNDERLYINGS', 'CRUDEOILM,NATURALGAS,SILVERM').split(',') if x.strip()]
TOP_K = int(_env('MCX_TOP_K', '4'))          # liquid strikes kept per underlying
RANGE = int(_env('MCX_DISCOVER_RANGE', '6'))  # ATM +/- this many, before ranking
N1 = int(_env('MCX_N1', '5'))
N2 = int(_env('MCX_N2', '8'))
Q = float(_env('MCX_Q', '0.90'))
WINDOW = int(_env('MCX_WINDOW', '120'))
MIN_SAMPLES = int(_env('MCX_MIN_SAMPLES', '40'))


def _hhmm(s, d):
    try:
        h, m = s.split(':')
        return dt_time(int(h), int(m))
    except Exception:
        return d


def _now():
    return datetime.now(IST)


def _token():
    """Read the Upstox token from the FEEDER's .env, freshly, every time.

    Two reasons this is not cached:
      1. The token lives in viz_hedge/.env — auto_token.py writes it there and this
         repo has no business owning a second copy that can drift.
      2. This service starts at 09:05 but prep does not mint the day's token until
         ~09:09. Re-reading each poll means the refresh is picked up automatically
         instead of the process running all session on an expired credential.
    """
    from dotenv import dotenv_values
    for path in (settings.FEEDER_ENV, ROOT / '.env'):
        try:
            v = dotenv_values(path).get('UPSTOX_ACCESS_TOKEN')
            if v and v.strip():
                return v.strip()
        except Exception:
            pass
    return os.getenv('UPSTOX_ACCESS_TOKEN')


def _token_valid(tok: str | None) -> bool:
    if not tok:
        return False
    try:
        r = requests.get('https://api.upstox.com/v2/user/profile',
                         headers={'Authorization': f'Bearer {tok}',
                                  'Accept': 'application/json'}, timeout=10)
        return r.ok
    except Exception:
        return False


def _wait_for_token(deadline_mins: int = 25) -> str:
    """Poll for a working token. Prep runs at ~09:09; we start at 09:05."""
    waited = 0
    while waited < deadline_mins * 60:
        tok = _token()
        if _token_valid(tok):
            if waited:
                logger.info(f'token became valid after {waited}s')
            return tok
        if waited == 0:
            logger.warning(f'no valid Upstox token yet in {settings.FEEDER_ENV} — '
                           f'waiting for the prep job to mint today\'s (checks every 30s)')
        time.sleep(30)
        waited += 30
    sys.exit(f'no valid token after {deadline_mins} min — check: '
             f'journalctl -u vizhedge-prep --since today')


def load_master() -> list:
    p = settings.NSE_JSON_PATH.parent / 'MCX.json'
    if not p.exists():
        sys.exit(f'{p} missing — run viz_hedge/utils/download_instruments.py MCX')
    return json.loads(p.read_text())


def quote(keys: list[str], token: str) -> dict:
    """Upstox full quote. Batched — one call for every instrument we track."""
    out = {}
    for i in range(0, len(keys), 100):          # stay well inside the URL/limit budget
        chunk = keys[i:i + 100]
        r = requests.get(QUOTE_URL, params={'instrument_key': ','.join(chunk)},
                         headers={'Authorization': f'Bearer {token}',
                                  'Accept': 'application/json'}, timeout=20)
        if not r.ok:
            logger.error(f'quote {r.status_code}: {r.text[:180]}')
            continue
        data = r.json().get('data', {}) or {}
        for v in data.values():
            k = v.get('instrument_token')
            if k:
                out[k] = v
    return out


def discover(master: list, token: str) -> dict[str, str]:
    """Pick the most LIQUID strikes, which are usually not the ATM ones.

    Ranks by traded volume from a single wide quote call rather than assuming ATM is
    busiest — on MCX the active strike is often a round number some distance away.
    """
    chosen = {}
    for und in UNDERLYINGS:
        rows = [r for r in master
                if r.get('instrument_type') in ('CE', 'PE')
                and und in (str(r.get('name', '')).upper(),
                            str(r.get('asset_symbol', '')).upper())
                and r.get('expiry') and r.get('strike_price')]
        if not rows:
            logger.warning(f'{und}: no option rows in MCX.json')
            continue
        exp = min({int(r['expiry']) for r in rows
                   if datetime.fromtimestamp(int(r['expiry']) / 1000, IST).date() >= _now().date()},
                  default=None)
        if exp is None:
            logger.warning(f'{und}: every expiry is in the past')
            continue
        same = [r for r in rows if int(r['expiry']) == exp]
        strikes = sorted({float(r['strike_price']) for r in same})
        mid = strikes[len(strikes) // 2]
        near = [r for r in same
                if abs(strikes.index(float(r['strike_price'])) - strikes.index(mid)) <= RANGE]
        q = quote([r['instrument_key'] for r in near], token)
        ranked = sorted(near, key=lambda r: -(q.get(r['instrument_key'], {}).get('volume') or 0))
        for r in ranked[:TOP_K]:
            v = q.get(r['instrument_key'], {}).get('volume') or 0
            chosen[r['instrument_key']] = r['trading_symbol']
            logger.info(f"  {und}: {r['trading_symbol']}  volume {v:,}")
        # the underlying future too — it leads the options
        fut = [r for r in master if r.get('instrument_type') == 'FUT'
               and und in (str(r.get('name', '')).upper(), str(r.get('asset_symbol', '')).upper())]
        if fut:
            f = sorted(fut, key=lambda r: int(r.get('expiry') or 0))[0]
            chosen[f['instrument_key']] = f['trading_symbol']
            logger.info(f"  {und}: {f['trading_symbol']}  (future)")
    return chosen


def write_points(write_api, quotes: dict, symbols: dict[str, str]) -> int:
    pts = []
    for key, v in quotes.items():
        ltp = v.get('last_price')
        if ltp is None:
            continue
        sym = symbols.get(key, key.replace('|', '_'))
        p = (Point(f'MCX_{sym}')
             .tag('segment', key.split('|', 1)[0]).tag('exch', 'MCX').tag('symbol', sym)
             .field('ltp', float(ltp))
             .time(datetime.now(timezone.utc), WritePrecision.MS))
        for f in ('volume', 'oi', 'average_price'):
            if isinstance(v.get(f), (int, float)):
                p.field({'volume': 'vtt', 'average_price': 'atp'}.get(f, f), float(v[f]))
        pts.append(p)
    if pts:
        try:
            write_api.write(bucket=settings.INFLUX_BUCKET, org=settings.INFLUX_ORG, record=pts)
        except Exception as e:
            logger.error(f'influx write failed ({len(pts)} pts): {e}')
            return 0
    return len(pts)


def main():
    start, stop = _hhmm(START, dt_time(9, 5)), _hhmm(STOP, dt_time(23, 35))
    now = _now()
    if now.time() >= stop:
        logger.info(f'past MCX_STOP ({STOP}) — nothing to do')
        return
    if now.time() < start:
        wait = (now.replace(hour=start.hour, minute=start.minute, second=0) - now).total_seconds()
        logger.info(f'waiting {wait:.0f}s until {START} IST')
        time.sleep(wait)

    token = _wait_for_token()
    master = load_master()
    logger.info(f'discovering liquid strikes for {UNDERLYINGS} (top {TOP_K} by volume)')
    symbols = discover(master, token)
    if not symbols:
        sys.exit('nothing to poll')
    keys = list(symbols)
    logger.info(f'polling {len(keys)} instrument(s) every {POLL_MINS}min, '
                f'signals every {BAR_MINS}min  (n1={N1} n2={N2} q={Q})')

    from src.config.influx import client
    write_api = client.write_api(write_options=SYNCHRONOUS)
    notifier = Notifier()
    hist: dict[str, list] = {}
    last_sig: dict[str, datetime] = {}
    stopping = {'v': False}
    _signal.signal(_signal.SIGTERM, lambda *_: stopping.update(v=True))
    _signal.signal(_signal.SIGINT, lambda *_: stopping.update(v=True))

    next_bar = time.monotonic() + BAR_MINS * 60
    while not stopping['v'] and _now().time() < stop:
        t0 = time.monotonic()
        tok = _token() or token
        q = quote(keys, tok)
        if not q:                                   # every call failed — likely a 401
            if not _token_valid(tok):
                logger.warning('token rejected mid-session — waiting for a refresh')
                token = _wait_for_token(deadline_mins=10)
                q = quote(keys, token)
        n = write_points(write_api, q, symbols)
        for k, v in q.items():
            if v.get('last_price') is not None:
                hist.setdefault(k, []).append(float(v['last_price']))
                hist[k] = hist[k][-(WINDOW + N2 + 5):]
        logger.info(f'polled {len(q)}/{len(keys)}, wrote {n} points')

        if time.monotonic() >= next_bar:
            next_bar = time.monotonic() + BAR_MINS * 60
            for k, series in hist.items():
                if len(series) < N2 + 1 + MIN_SAMPLES:
                    continue
                r = angle_series(np.asarray(series, float), N1, N2, 'pct')
                ang = r['angle_deg']
                if ang.size == 0:
                    continue
                thr = adaptive_threshold_latest(ang, 'percentile', WINDOW, Q,
                                                min_periods=MIN_SAMPLES)
                if thr is None or thr != thr or ang[-1] < thr:
                    continue
                if not bool(is_upward_bend(r['slope_base'][-1:], r['slope_full'][-1:],
                                           r['slope_recent'][-1:], True)[0]):
                    continue
                prev = last_sig.get(k)
                if prev and (_now() - prev).total_seconds() < BAR_MINS * 60 * 2:
                    continue
                last_sig[k] = _now()
                sig = Signal(instrument_key=k, symbol=symbols[k],
                             action=SignalAction.ENTER_LONG, price=series[-1],
                             strategy='mcx_slope_angle',
                             reason=f'angle {ang[-1]:.2f} >= {thr:.2f} '
                                    f'[{BAR_MINS:g}min bars, n1={N1} n2={N2}]',
                             meta={'angle_deg': float(ang[-1]), 'threshold_deg': float(thr),
                                   'slope_recent': float(r['slope_recent'][-1]),
                                   'n1': N1, 'n2': N2})
                logger.info(f'MCX SIGNAL {sig.symbol} @ {sig.price} — {sig.reason}')
                notifier.notify(sig)
                _journal(sig)

        sleep = max(5.0, POLL_MINS * 60 - (time.monotonic() - t0))
        for _ in range(int(sleep)):
            if stopping['v']:
                break
            time.sleep(1)

    notifier.close()
    logger.info('mcx poller stopped')


def _journal(sig: Signal) -> None:
    d = Path(settings.JOURNAL_DIR) / _now().strftime('%Y%m%d')
    d.mkdir(parents=True, exist_ok=True)
    with open(d / 'mcx_signals.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(sig.to_dict(), default=str) + '\n')


if __name__ == '__main__':
    main()
