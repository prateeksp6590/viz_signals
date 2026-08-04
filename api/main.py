"""
viz_signals API — REST + live WebSocket, single user, no auth.

Auth is deliberately absent: access is over Tailscale, so the service is only
reachable from your own devices and never binds to a public interface. If that ever
changes, auth stops being optional.

    uvicorn api.main:app --host 127.0.0.1 --port 8000

Endpoints
  GET  /api/health              feed + hub status
  GET  /api/instruments         what is streaming, with last tick and count
  GET  /api/ticks               downsampled OHLC for one instrument
  GET  /api/signals             today's signals (optionally one symbol)
  GET  /api/pnl                 today's closed trades, sized, with charges
  POST /api/detect              re-run the detector with different n1/n2/q
  WS   /ws                      live ticks + signals
"""

import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.home import build_rows
from api.hub import Hub
from src.config import settings
from src.services.influx_reader import InfluxReader
from src.strategies.angle_math import (adaptive_threshold, angle_series,
                                       is_upward_bend)
from src.utils.logger import logger
from src.utils.sizing import quantity_for, underlying_of

IST = timezone(timedelta(hours=5, minutes=30))
STATIC = Path(__file__).parent / 'static'

_hub: Hub | None = None
_symbol_map: dict[str, str] = {}


def _load_symbol_map() -> dict[str, str]:
    keys, out = set(settings.DISPLAY_INSTRUMENTS), {}
    for ex in ('NSE', 'BSE', 'MCX'):
        p = settings.NSE_JSON_PATH.parent / f'{ex}.json'
        if not p.exists():
            continue
        try:
            for r in json.loads(p.read_text()):
                if r.get('instrument_key') in keys:
                    out[r['instrument_key']] = r['trading_symbol']
        except Exception as e:
            logger.warning(f'{p}: {e}')
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown. `@app.on_event` was removed in Starlette 1.x — this is the
    supported form and works on both old and new versions."""
    global _hub, _symbol_map
    _symbol_map = _load_symbol_map()
    _hub = Hub(_symbol_map, interval=float(settings.POLL_INTERVAL_SECS or 1),
               bar_secs=settings.STREAM_BAR_SECS,
               raw_ticks=settings.STREAM_RAW_TICKS)
    await _hub.start()
    logger.info(f'API up — {len(_symbol_map)} instruments, hub every {_hub._interval}s')
    try:
        yield
    finally:
        if _hub:
            await _hub.stop()


app = FastAPI(title='viz_signals API', version='0.1', lifespan=lifespan)


def _reader() -> InfluxReader:
    return InfluxReader(_symbol_map)


# ── REST ─────────────────────────────────────────────────────────────────────

@app.get('/api/health')
def health():
    return {'ok': True, 'instruments': len(_symbol_map),
            'client_id': os.getenv('UPSTOX_CLIENT_ID', ''),
            'client_name': os.getenv('UPSTOX_CLIENT_NAME', ''),
            'clients': _hub.n_clients if _hub else 0,
            'hub_error': _hub.last_error if _hub else None,
            'bucket': settings.INFLUX_BUCKET, 'mode': settings.ORDER_MODE,
            'stale_bundle': _stale_bundle(),
            'now': datetime.now(IST).isoformat()}


@app.get('/api/instruments')
def instruments(minutes: int = Query(1440, ge=1, le=10080)):
    r = _reader()
    try:
        now = datetime.now(timezone.utc)
        got = r.fetch_many(settings.DISPLAY_INSTRUMENTS,
                           {k: now - timedelta(minutes=minutes)
                            for k in settings.DISPLAY_INSTRUMENTS})
        out = []
        for k, df in got.items():
            if df.empty or 'ltp' not in df:
                continue
            out.append({'key': k, 'symbol': _symbol_map.get(k, k),
                        'segment': k.split('|', 1)[0],
                        'underlying': underlying_of(k, _symbol_map.get(k, '')),
                        'qty': quantity_for(k, _symbol_map.get(k, ''))[0],
                        'ticks': int(len(df)),
                        'last_ltp': float(df['ltp'].iloc[-1]),
                        'last_tick': df.index[-1].astimezone(IST).isoformat()})
        return sorted(out, key=lambda x: x['symbol'])
    finally:
        r.close()


@app.get('/api/home')
def home(lookback: int = Query(30, ge=5, le=240)):
    """The whole Home tab in one call: status, LTP/LTQ/VTT, trend, trigger, P&L."""
    r = _reader()
    try:
        rows = build_rows(r, _symbol_map, lookback_min=lookback)
        tot = {'realised': 0.0, 'open': 0.0, 'total': 0.0}
        for x in rows:
            if x['pnl']:
                for f in tot:
                    tot[f] += x['pnl'][f]
        return {'rows': rows, 'totals': tot,
                'live': sum(1 for x in rows if x['status'] == 'live'),
                'n': len(rows), 'as_of': datetime.now(IST).isoformat()}
    finally:
        r.close()


@app.get('/api/ticks')
def ticks(key: str, minutes: int = Query(60, ge=1, le=1440),
          bucket: str = Query('1min', pattern=r'^\d+(s|min|h)$')):
    """OHLC, aggregated SERVER-SIDE. A phone must never receive 30k raw ticks."""
    r = _reader()
    try:
        now = datetime.now(timezone.utc)
        df = r.fetch_range(key, now - timedelta(minutes=minutes), now)
        if df.empty or 'ltp' not in df:
            return {'key': key, 'bars': []}
        o = df['ltp'].resample(bucket).ohlc().dropna(how='all')
        if 'vtt' in df:
            o['volume'] = df['vtt'].resample(bucket).last().diff().clip(lower=0)
        o = o.reset_index()
        o['_time'] = o['_time'].dt.tz_convert(IST).astype(str)
        return {'key': key, 'symbol': _symbol_map.get(key, key),
                'bars': o.replace({np.nan: None}).to_dict('records')}
    finally:
        r.close()


def _journal(name: str, date: str | None = None) -> list[dict]:
    d = date or datetime.now(IST).strftime('%Y%m%d')
    f = Path(settings.JOURNAL_DIR) / d / f'{name}.jsonl'
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


@app.get('/api/signals')
def signals(date: str | None = None, symbol: str | None = None, limit: int = 200):
    rows = _journal('signals', date)
    if symbol:
        rows = [r for r in rows if r.get('symbol') == symbol]
    return rows[-limit:][::-1]


@app.get('/api/pnl')
def pnl(date: str | None = None):
    closed = [p for p in _journal('positions', date)
              if p.get('event') == 'close' and p.get('realized_pnl') is not None]
    trades, by_u = [], {}
    for p in closed:
        k, sym = p.get('instrument_key', ''), p.get('symbol', '')
        qty = int(p.get('qty') or 0) or quantity_for(k, sym)[0]
        gross = ((float(p['exit_price']) - float(p['avg_entry']))
                 * (1 if p.get('side') == 'LONG' else -1) * qty)
        u = underlying_of(k, sym)
        d = by_u.setdefault(u, {'trades': 0, 'gross': 0.0})
        d['trades'] += 1; d['gross'] += gross
        trades.append({'symbol': sym, 'side': p.get('side'), 'qty': qty,
                       'entry': float(p['avg_entry']), 'exit': float(p['exit_price']),
                       'gross': gross, 'entry_ts': p.get('entry_ts'),
                       'exit_ts': p.get('exit_ts')})
    return {'trades': trades, 'by_underlying': by_u,
            'total_gross': sum(t['gross'] for t in trades), 'n': len(trades)}


@app.post('/api/detect')
def detect(key: str, minutes: int = 120, n1: int = 50, n2: int = 80,
           q: float = 0.95, window: int = 2000, price_mode: str = 'pct',
           require_convex: bool = True):
    """Re-run the detector with different parameters over recent ticks.

    Calls the SAME angle_math the live engine and the backtester use, so what you
    see here is what would actually have fired.
    """
    if not 0 < n1 < n2:
        raise HTTPException(400, 'require 0 < n1 < n2')
    r = _reader()
    try:
        now = datetime.now(timezone.utc)
        df = r.fetch_range(key, now - timedelta(minutes=minutes), now)
        if df.empty or 'ltp' not in df:
            return {'key': key, 'triggers': [], 'n_ticks': 0}
        p = df['ltp'].to_numpy(float)
        res = angle_series(p, n1, n2, price_mode)
        ang, idx = res['angle_deg'], res['index']
        thr = adaptive_threshold(ang, 'percentile', window, q)
        up = is_upward_bend(res['slope_base'], res['slope_full'],
                            res['slope_recent'], require_convex)
        fire = np.isfinite(ang) & np.isfinite(thr) & (ang >= thr) & up
        t = df.index
        out = [{'t': t[idx[k]].astimezone(IST).isoformat(),
                'ltp': float(p[idx[k]]), 'angle': float(ang[k]),
                'threshold': float(thr[k])}
               for k in np.where(fire)[0]]
        return {'key': key, 'symbol': _symbol_map.get(key, key), 'n_ticks': len(p),
                'params': {'n1': n1, 'n2': n2, 'q': q, 'window': window,
                           'price_mode': price_mode, 'convex': require_convex},
                'n_triggers': len(out), 'triggers': out[-300:]}
    finally:
        r.close()


# ── live stream ──────────────────────────────────────────────────────────────

@app.websocket('/ws')
async def ws(sock: WebSocket):
    await sock.accept()
    q = _hub.subscribe()
    logger.info(f'ws client connected ({_hub.n_clients} total)')
    try:
        await sock.send_json({'type': 'hello',
                              'instruments': len(_symbol_map),
                              'interval': _hub._interval,
                              'bar_secs': _hub._bar_secs})
        while True:
            msg = await q.get()
            await sock.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f'ws error: {e}')
    finally:
        _hub.unsubscribe(q)
        logger.info(f'ws client gone ({_hub.n_clients} left)')


if STATIC.exists():
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

APP_DIR = STATIC / 'app'


def _stale_bundle() -> str | None:
    """Is the built bundle older than the source it was built from?

    api/static/app is a build artifact committed to git. Editing frontend/src does
    nothing until `npm run build` runs, and the failure is silent — the dashboard
    simply keeps showing the old behaviour. Surfacing it in /api/health turns a
    confusing 'my change did not appear' into a one-line answer.
    """
    src = APP_DIR.parent.parent.parent / 'frontend' / 'src'
    idx = APP_DIR / 'index.html'
    if not src.exists() or not idx.exists():
        return None
    newest = max((f.stat().st_mtime for f in src.rglob('*') if f.is_file()), default=0)
    if newest > idx.stat().st_mtime + 5:
        from datetime import datetime as _dt
        return (f'frontend/src changed at '
                f'{_dt.fromtimestamp(newest, IST):%Y-%m-%d %H:%M} but the bundle was '
                f'built at {_dt.fromtimestamp(idx.stat().st_mtime, IST):%Y-%m-%d %H:%M} '
                f'— run: cd frontend && npm run build')
    return None


if APP_DIR.exists():
    # html=True makes any unknown path fall back to index.html, which a single-page
    # app needs so a deep link or a PWA cold start does not 404.
    app.mount('/app', StaticFiles(directory=APP_DIR, html=True), name='app')


@app.get('/')
def index():
    if APP_DIR.exists():
        from fastapi.responses import RedirectResponse
        return RedirectResponse('/app/')
    return FileResponse(STATIC / 'index.html')
