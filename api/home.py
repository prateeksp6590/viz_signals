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
from src.utils.sizing import quantity_for, underlying_of

IST = timezone(timedelta(hours=5, minutes=30))

# Tick rates differ by an order of magnitude, so a fixed tick window would mean two
# minutes on NIFTY and half an hour on a thin MCX strike.
TREND_WINDOW = {'NSE': 200, 'BSE': 200, 'MCX': 35}
LIVE_SECS = 60          # ticked within this = live; anything else is not live


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


def build_rows(reader, symbol_map: dict[str, str], lookback_min: int = 30) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    keys = settings.ANALYZE_INSTRUMENTS
    got = reader.fetch_many(keys, {k: now_utc - timedelta(minutes=lookback_min) for k in keys})
    date = datetime.now(IST).strftime('%Y%m%d')

    marks = {k: float(df['ltp'].iloc[-1]) for k, df in got.items()
             if not df.empty and 'ltp' in df}
    pnl = pnl_by_instrument(date, marks)
    trig = last_trigger_by_instrument(date)

    rows = []
    for i, k in enumerate(keys, 1):
        df = got.get(k)
        sym = symbol_map.get(k, k)
        exch = k.split('|', 1)[0].split('_', 1)[0]
        has = df is not None and not df.empty and 'ltp' in df
        age = (now_utc - df.index[-1].to_pydatetime()).total_seconds() if has else None
        row = {'sr': i, 'key': k, 'symbol': sym, 'segment': k.split('|', 1)[0],
               'status': 'live' if (age is not None and age <= LIVE_SECS) else 'not live',
               'age_s': round(age, 1) if age is not None else None,
               'ltp': None, 'ltq': None, 'vtt': None,
               'trend': {'level': 0, 'label': 'Neutral', 'ready': False},
               'trigger': trig.get(k),
               'pnl': pnl.get(k, {'realised': 0.0, 'open': 0.0, 'total': 0.0, 'open_qty': 0})
                      if k in pnl else None,
               'qty': quantity_for(k, sym)[0],
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
