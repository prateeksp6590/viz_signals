#!/usr/bin/env python3
"""What did volume / OI / trade size look like at the ENTRY of each trade?

    python utils/trade_conditions.py --date 20260813

WHY THIS IS BUILT AS A COMPARISON, NOT A LOOK AT THE WINNERS
------------------------------------------------------------
The natural question is "what did the two big winners have in common". With ~7
fields and n=2 the answer is always "several things", none of which mean anything.
So this scores EVERY closed trade on the same features and reports winners against
losers side by side. A feature only counts if it separates them.

Even then, treat the output as a lead and nothing more. On 2026-08-13 there were 36
trades and the top 5 were 608% of net -- the "winner" group is 5 observations. Seven
features across 5 observations will produce an apparent pattern by chance almost
every time. The AUC column is there to keep that honest: 0.50 means no separation.

A prior worth holding while reading it: an identical study on these fields
(strategy_research.ipynb, 18 SENSEX strike-days) found that a pure TIME-OF-DAY
control out-scored volume and book imbalance, at AUC 0.707 vs 0.577 and 0.582, and
neither field was stable across days. minute_of_day is included below for the same
reason. If it separates winners from losers as well as volume does, the "signal" is
the clock.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings                              # noqa: E402
from influxdb_client import InfluxDBClient                   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
FIELDS = ['ltp', 'vtt', 'ltq', 'tbq', 'tsq', 'oi']


def _read(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def load_fields(measurement: str, date_str: str) -> pd.DataFrame:
    d = datetime.strptime(date_str, '%Y%m%d').date()
    fl = ' or '.join(f'r._field == "{f}"' for f in FIELDS)
    q = (f'from(bucket: "{settings.INFLUX_BUCKET}")\n'
         f'  |> range(start: {d}T00:00:00+05:30, '
         f'stop: {d + timedelta(days=1)}T00:00:00+05:30)\n'
         f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
         f'  |> filter(fn: (r) => {fl})\n'
         f'  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")')
    with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                        org=settings.INFLUX_ORG, timeout=300_000) as c:
        df = c.query_api().query_data_frame(q)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True)
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.set_index(pd.to_datetime(df['_time']).dt.tz_convert(IST)).sort_index()
    return df[[c for c in FIELDS if c in df.columns]].astype(float)


def features_at(raw: pd.DataFrame, when: pd.Timestamp, lookback_min: int = 5) -> dict:
    """Feature values in the minutes BEFORE `when`. Strictly causal: nothing at or
    after the entry timestamp is used, so these are values the engine could have
    seen when it decided."""
    w = raw.loc[:when]
    if len(w) < 50:
        return {}
    recent = w.loc[when - pd.Timedelta(minutes=lookback_min):]
    prior = w.loc[:when - pd.Timedelta(minutes=lookback_min)]
    if len(recent) < 5 or len(prior) < 50:
        return {}

    out = {}
    if 'vtt' in w:
        # per-minute traded volume in the recent window vs the session so far
        rv = (recent['vtt'].iloc[-1] - recent['vtt'].iloc[0]) / max(lookback_min, 1)
        pm = max((prior.index[-1] - prior.index[0]).total_seconds() / 60, 1)
        pv = (prior['vtt'].iloc[-1] - prior['vtt'].iloc[0]) / pm
        out['vol_ratio'] = rv / pv if pv > 0 else np.nan
    if 'ltq' in w:
        out['ltq_max'] = float(recent['ltq'].max())
        out['ltq_vs_prior'] = (float(recent['ltq'].max())
                               / max(float(prior['ltq'].median()), 1))
    if {'tbq', 'tsq'} <= set(w.columns):
        tb, ts = float(recent['tbq'].mean()), float(recent['tsq'].mean())
        out['imbalance'] = (tb - ts) / (tb + ts) if (tb + ts) > 0 else np.nan
        ptb, pts = float(prior['tbq'].mean()), float(prior['tsq'].mean())
        pimb = (ptb - pts) / (ptb + pts) if (ptb + pts) > 0 else np.nan
        out['imbalance_shift'] = out['imbalance'] - pimb
    if 'oi' in w:
        o0, o1 = float(recent['oi'].iloc[0]), float(recent['oi'].iloc[-1])
        out['oi_chg_pct'] = 100 * (o1 - o0) / o0 if o0 > 0 else np.nan
    if 'ltp' in w:
        p = recent['ltp']
        out['ret_5m_pct'] = 100 * (p.iloc[-1] / p.iloc[0] - 1) if p.iloc[0] > 0 else np.nan
        out['range_5m_pct'] = 100 * (p.max() - p.min()) / p.iloc[0] if p.iloc[0] > 0 else np.nan
    # CONTROL, not a feature -- see module docstring
    out['minute_of_day'] = when.hour * 60 + when.minute
    return out


def _auc(pos, neg) -> float:
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    pos, neg = pos[np.isfinite(pos)], neg[np.isfinite(neg)]
    if len(pos) < 3 or len(neg) < 3:
        return np.nan
    r = pd.Series(np.concatenate([pos, neg])).rank().values
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2)
                 / (len(pos) * len(neg)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--date', required=True, help='YYYYMMDD')
    ap.add_argument('--top', type=int, default=5, help='size of the winner group')
    ap.add_argument('--lookback', type=int, default=5, help='minutes before entry')
    a = ap.parse_args()

    d = Path(settings.JOURNAL_DIR) / a.date
    poss = [p for p in _read(d / 'positions.jsonl')
            if p.get('event') == 'close' and p.get('realized_pnl') is not None]
    if not poss:
        print(f'no closed positions in {d}')
        return 1

    cache, rows = {}, []
    for p in poss:
        sym = p.get('symbol', '')
        meas = f"{p.get('instrument_key','').split('|')[0].split('_')[0]}_{sym}"
        if meas not in cache:
            cache[meas] = load_fields(meas, a.date)
        raw = cache[meas]
        if raw.empty:
            continue
        t = pd.to_datetime(p['entry_ts'])
        if t.tzinfo is None:
            t = t.tz_localize('UTC')
        t = t.tz_convert(IST)
        f = features_at(raw, t, a.lookback)
        if not f:
            continue
        rows.append({'symbol': sym[:28], 'entry': t.strftime('%H:%M:%S'),
                     'pnl': float(p['realized_pnl']), **f})

    if len(rows) < 8:
        print(f'only {len(rows)} trades with usable feature windows — too few')
        return 1

    df = pd.DataFrame(rows).sort_values('pnl', ascending=False)
    feats = [c for c in df.columns if c not in ('symbol', 'entry', 'pnl')]
    n = min(a.top, len(df) // 3)

    print(f'\n{a.date}: {len(df)} trades, {a.lookback}-minute window before entry\n')
    print('── TOP', n, 'BY P&L')
    print(df.head(n)[['symbol', 'entry', 'pnl'] + feats].round(3).to_string(index=False))
    print('\n── BOTTOM', n, 'BY P&L')
    print(df.tail(n)[['symbol', 'entry', 'pnl'] + feats].round(3).to_string(index=False))

    win, lose = df.head(n), df.tail(n)
    rest = df.iloc[n:-n]
    out = []
    for f in feats:
        out.append({'feature': f,
                    'top_median': float(np.nanmedian(win[f])),
                    'mid_median': float(np.nanmedian(rest[f])) if len(rest) else np.nan,
                    'bot_median': float(np.nanmedian(lose[f])),
                    'AUC_top_vs_bot': round(_auc(win[f], lose[f]), 3)})
    res = pd.DataFrame(out)
    res['abs_edge'] = (res['AUC_top_vs_bot'] - 0.5).abs()
    print('\n── WINNERS vs LOSERS')
    print(res.sort_values('abs_edge', ascending=False).round(3).to_string(index=False))

    print(f'\nAUC 0.50 = the feature does not separate winners from losers at all.')
    print(f'MEASURED null distribution for groups of this size (20k simulations of')
    print(f'two samples drawn from the SAME distribution):')
    print(f'    5 vs 5     AUC >= 0.70 happens 15% of the time, >= 0.80  7%')
    print(f'   10 vs 10               7%                            1%')
    print(f'   18 vs 18               2%                          0.1%')
    print(f'With {n} vs {n} and {len(feats)} features, expect one or two to clear 0.70')
    print(f'with nothing behind them. Do not act on a single day of this.')
    print('Check minute_of_day: if it separates as well as the order-flow features,')
    print('the pattern is the clock, which is what an 18-series study already found.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
