"""
Everything the Home table needs, in one call.

P&L is REALISED + OPEN per instrument: a Rs 100 realised gain with a Rs 20 open loss
shows Rs 80. That combined view is deliberate — today's daily-loss-limit bug came from
realised and open being tracked separately and only one of them being checked.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from src.config import settings
from src.strategies.angle_math import trend
from src.utils.moneyness import classify
from src.utils.sizing import quantity_for, underlying_of

IST = timezone(timedelta(hours=5, minutes=30))

# Tick rates differ by an order of magnitude, so a fixed tick window would mean two
# minutes on NIFTY and half an hour on a thin MCX strike.
TREND_WINDOW = {'NSE': 200, 'BSE': 200, 'MCX': 35}
# 'Live' cannot be a fixed number of seconds. A deep-ITM SENSEX put trades ~947 times
# a session — one tick every ~24s — so a 60s rule makes a perfectly healthy leg flicker
# between live and not-live all day, while a NIFTY ATM leg ticking 34,000 times would
# tolerate a 60s stall that is genuinely an incident.
# So: live if it has ticked within max(LIVE_SECS, LIVE_GAP_MULT x its own median gap).
LIVE_SECS = 60
LIVE_GAP_MULT = 6


def _journal(name: str, date: str) -> list[dict]:
    f = Path(settings.JOURNAL_DIR) / date / f'{name}.jsonl'
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]


def pnl_by_instrument(date: str, marks: dict[str, float]) -> dict[str, dict]:
    """{instrument_key: {realised, open, total, open_qty}} — realised + open.

    Open positions are reconstructed from the journal (an 'open' with no later
    'close'), because the API runs in a different process from the engine and cannot
    read its PositionTracker.
    """
    out: dict[str, dict] = {}
    live: dict[str, list] = {}
    for r in _journal('positions', date):
        k = r.get('instrument_key') or ''
        if not k:
            continue
        d = out.setdefault(k, {'realised': 0.0, 'open': 0.0, 'total': 0.0, 'open_qty': 0})
        if r.get('event') == 'close':
            if r.get('realized_pnl') is not None:
                d['realised'] += float(r['realized_pnl'])
            if live.get(k):
                live[k].pop()
        elif r.get('event') == 'open':
            live.setdefault(k, []).append(r)

    for k, rows in live.items():
        d = out.setdefault(k, {'realised': 0.0, 'open': 0.0, 'total': 0.0, 'open_qty': 0})
        mark = marks.get(k)
        for r in rows:
            qty = int(r.get('qty') or 0)
            d['open_qty'] += qty
            if mark is not None and r.get('avg_entry'):
                sign = 1 if r.get('side') == 'LONG' else -1
                d['open'] += (mark - float(r['avg_entry'])) * sign * qty
    for d in out.values():
        d['total'] = d['realised'] + d['open']
    return out


def last_trigger_by_instrument(date: str) -> dict[str, dict]:
    out = {}
    for s in _journal('signals', date):
        k = s.get('instrument_key')
        if k and s.get('action', '').startswith('ENTER'):
            m = s.get('meta') or {}
            out[k] = {'t': s.get('ts'), 'action': s.get('action'),
                      'price': s.get('price'),
                      'angle': m.get('angle_deg'), 'threshold': m.get('threshold_deg')}
    return out


def last_known(reader, symbol_map: dict[str, str]) -> dict[str, dict]:
    """Last price of the DAY per instrument, in one query.

    The rolling lookback is sized for the trend calculation, so after 15:30 it holds
    nothing for NSE/BSE and every row would show a dash where the closing price
    belongs. A single `last()` over the session, filtered on the indexed `segment`
    tag, fills that in for ~the cost of one small query.
    """
    keys = settings.DISPLAY_INSTRUMENTS
    if not keys:
        return {}
    segs = sorted({k.split('|', 1)[0] for k in keys})
    cond = ' or '.join(f'r.segment == "{x}"' for x in segs)
    day = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    flux = (f'from(bucket:"{settings.INFLUX_BUCKET}")\n'
            f'  |> range(start: {day.isoformat()})\n'
            f'  |> filter(fn: (r) => {cond})\n'
            f'  |> filter(fn: (r) => r._field == "ltp")\n'
            f'  |> last()\n'
            f'  |> keep(columns: ["_measurement", "_value", "_time"])')
    try:
        df = reader._query_api.query_data_frame(flux)
    except Exception:
        return {}
    import pandas as pd
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
    if df is None or df.empty or '_measurement' not in df.columns:
        return {}
    by_meas = {r['_measurement']: {'ltp': float(r['_value']),
                                   't': pd.to_datetime(r['_time'], utc=True).to_pydatetime()}
               for _, r in df.iterrows()}
    out = {}
    for k in keys:
        m = reader.measurement_name(k)
        if m in by_meas:
            out[k] = by_meas[m]
    return out


def build_rows(reader, symbol_map: dict[str, str], lookback_min: int = 30) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    keys = settings.DISPLAY_INSTRUMENTS
    got = reader.fetch_many(keys, {k: now_utc - timedelta(minutes=lookback_min) for k in keys})
    date = datetime.now(IST).strftime('%Y%m%d')

    lastk = last_known(reader, symbol_map)
    # classify from the freshest premium available (live tick, else the close)
    _px = {}
    for k in keys:
        df = got.get(k)
        v = (float(df['ltp'].iloc[-1]) if df is not None and not df.empty and 'ltp' in df
             else (lastk.get(k) or {}).get('ltp'))
        _px[k] = (symbol_map.get(k, k), v)
    mny = classify(_px)
    marks = {k: float(df['ltp'].iloc[-1]) for k, df in got.items()
             if not df.empty and 'ltp' in df}
    for k, v in lastk.items():                 # mark closed positions at the close
        marks.setdefault(k, v['ltp'])
    pnl = pnl_by_instrument(date, marks)
    trig = last_trigger_by_instrument(date)

    rows = []
    for i, k in enumerate(keys, 1):
        df = got.get(k)
        sym = symbol_map.get(k, k)
        exch = k.split('|', 1)[0].split('_', 1)[0]
        has = df is not None and not df.empty and 'ltp' in df
        lk = lastk.get(k)
        age = ((now_utc - df.index[-1].to_pydatetime()).total_seconds() if has
               else (now_utc - lk['t']).total_seconds() if lk else None)
        # tolerance from the instrument's OWN cadence, measured over the window we
        # already fetched — no extra query
        gap = None
        if has and len(df) > 5:
            deltas = np.diff(df.index.values).astype('timedelta64[ms]').astype(float) / 1000
            if deltas.size:
                gap = float(np.median(deltas))
        tol = max(LIVE_SECS, LIVE_GAP_MULT * gap) if gap else LIVE_SECS
        row = {'sr': i, 'key': k, 'symbol': sym, 'segment': k.split('|', 1)[0],
               'status': 'live' if (age is not None and age <= tol) else 'not live',
               'age_s': round(age, 1) if age is not None else None,
               'median_gap_s': round(gap, 1) if gap else None,
               'live_tol_s': round(tol, 1),
               'ltp': lk['ltp'] if lk else None,      # closing price when not live
               'ltq': None, 'vtt': None,
               'stale': not has and lk is not None,
               'trend': {'level': 0, 'label': 'Neutral', 'ready': False},
               'trigger': trig.get(k),
               'pnl': pnl.get(k, {'realised': 0.0, 'open': 0.0, 'total': 0.0, 'open_qty': 0})
                      if k in pnl else None,
               'qty': quantity_for(k, sym)[0],
               'moneyness': mny.get(k, 'UNKNOWN'),
               'traded': (k in settings.ANALYZE_INSTRUMENTS
                          and (not settings.ANALYZE_MONEYNESS
                               or mny.get(k, 'UNKNOWN') in settings.ANALYZE_MONEYNESS
                               or mny.get(k) == 'UNKNOWN')),
               'why_not': (None if k in settings.ANALYZE_INSTRUMENTS
                           else 'watch only'),
               'underlying': underlying_of(k, sym)}
        if has:
            row['ltp'] = float(df['ltp'].iloc[-1])
            for f in ('ltq', 'vtt'):
                if f in df.columns:
                    v = df[f].dropna()
                    row[f] = float(v.iloc[-1]) if len(v) else None
            row['trend'] = trend(df['ltp'].dropna().to_numpy(float),
                                 window=TREND_WINDOW.get(exch, 200),
                                 n1=settings.ANGLE_N1, n2=settings.ANGLE_N2,
                                 price_mode=settings.ANGLE_PRICE_MODE)
        rows.append(row)
    return rows
