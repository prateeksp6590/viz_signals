#!/usr/bin/env python3
"""Is there a variance risk premium to harvest on SENSEX weeklies, intraday?

    python utils/vol_premium.py --date 20260811,20260812,20260813,20260817,20260818

THE QUESTION
------------
An iron condor and a long straddle are opposite bets on ONE variable: realized vol
against implied vol. Neither structure is an edge. So before designing any structure,
measure whether the premium exists in YOUR market at YOUR horizon.

TWO MEASUREMENTS, AND WHY THE FIRST ONE IS THE REAL ANSWER
----------------------------------------------------------
1. STRADDLE DECAY (primary, in rupees). Sell the ATM straddle at time t, buy it back
   at t+H. That is the trade, priced by the market, with theta and gamma and the
   smile already in it. It needs no vol model and no annualization convention, so
   there is nothing to get subtly wrong. If short vol pays intraday, this is positive
   before costs and still positive after them.

2. IV vs REALIZED (diagnostic, in vol units). Reported under BOTH trading-time and
   calendar-time annualization, because the choice moves the answer by ~2.4x and
   there is no universally correct one: the market prices with calendar time to
   expiry, while realized vol only accumulates during trading hours. If the sign of
   the conclusion depends on which convention is used, the diagnostic is worthless
   and only measurement 1 counts. Printing one number here would have hidden that.

THE TWO TRAPS THIS CODE IS BUILT AROUND
---------------------------------------
OVERLAPPING WINDOWS. Sampling every 5 minutes with H=120 gives windows that share
96% of their data. 200 such samples are not 200 observations; a 375-minute session
holds about 3 independent 120-minute windows. Quoting the overlapping count is the
same error as "30 series" that turned out to be 5 sessions. Every headline below is
computed on NON-OVERLAPPING windows, with the overlapping count shown separately so
the gap is visible.

THE TAIL. Short vol wins most days and loses big occasionally, so a mean is close to
meaningless on a few sessions. The worst window, the 5th percentile, and the share of
total P&L coming from the worst 5% are printed for that reason. A strategy whose
edge disappears when you remove one window is a strategy that has not met its bad day
yet. Read those lines before the mean.

EXPIRY DAY IS A DIFFERENT TRADE. Theta and gamma both explode near expiry, so the
output is split by days-to-expiry. Pooling them would average two unrelated regimes.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# MUST be first: settings.py reads os.environ and never loads .env, so without this
# the script runs on built-in defaults with an empty token and 401s on every query.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                             # noqa: E402,F401

from src.config import settings                              # noqa: E402
from influxdb_client import InfluxDBClient                   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
# 385, not 375: the equity derivatives session runs 09:15-15:40 since 2026-08-03
# (SEBI Closing Auction Session). Data collected before that date is 375 minutes, so
# a mixed sample is annualised ~1.3% high — immaterial next to everything else here,
# but wrong is wrong and it costs nothing to be right.
SESSION_MIN = 385
YEAR_MIN_TRADING = SESSION_MIN * 250
YEAR_MIN_CALENDAR = 365 * 24 * 60
LOT = {'SENSEX': 20, 'NIFTY': 75, 'BANKNIFTY': 35}

# BSE_SENSEX 77400 PE 20 AUG 26  ->  strike 77400, side PE, expiry '20 AUG 26'
OPT_RE = re.compile(r'^[A-Z]+_(?P<und>[A-Z]+)\s+(?P<strike>\d+(?:\.\d+)?)\s+'
                    r'(?P<side>CE|PE)\s+(?P<exp>.+?)\s*$')


def load_day(date_str: str, segment: str) -> pd.DataFrame:
    """1-minute closes of ltp and iv for every instrument in `segment`.

    Filters on the SEGMENT TAG, not on measurement-name matching: a measured 137ms
    vs 2,329ms on this box. aggregateWindow collapses ~20k ticks per instrument to
    375 rows before anything crosses the wire.
    """
    d = datetime.strptime(date_str, '%Y%m%d').date()
    q = (f'from(bucket: "{settings.INFLUX_BUCKET}")\n'
         f'  |> range(start: {d}T00:00:00+05:30, '
         f'stop: {d + timedelta(days=1)}T00:00:00+05:30)\n'
         f'  |> filter(fn: (r) => r.segment == "{segment}")\n'
         f'  |> filter(fn: (r) => r._field == "ltp" or r._field == "iv")\n'
         f'  |> aggregateWindow(every: 1m, fn: last, createEmpty: false)\n'
         f'  |> keep(columns: ["_time", "_measurement", "_field", "_value"])')
    with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                        org=settings.INFLUX_ORG, timeout=600_000) as c:
        df = c.query_api().query_data_frame(q)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else None
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df['_time'] = pd.to_datetime(df['_time']).dt.tz_convert(IST)
    return df[['_time', '_measurement', '_field', '_value']]


def build_chain(df: pd.DataFrame):
    """-> (ce_ltp, pe_ltp, ce_iv, pe_iv) wide frames, index=minute, columns=strike."""
    meta = {}
    for m in df['_measurement'].unique():
        g = OPT_RE.match(str(m))
        if g:
            meta[m] = (float(g['strike']), g['side'], g['exp'].strip(), g['und'])
    if not meta:
        return None
    sub = df[df['_measurement'].isin(meta)].copy()
    sub['strike'] = sub['_measurement'].map(lambda m: meta[m][0])
    sub['side'] = sub['_measurement'].map(lambda m: meta[m][1])
    wide = sub.pivot_table(index='_time', columns=['side', '_field', 'strike'],
                           values='_value', aggfunc='last').sort_index()

    def part(side, field):
        if (side, field) not in wide.columns.droplevel(2).unique():
            return pd.DataFrame()
        return wide[side][field]

    expiry = sorted({v[2] for v in meta.values()})
    und = sorted({v[3] for v in meta.values()})
    # explicit names, not positional juggling: an earlier draft returned these in a
    # different order than the caller unpacked, which silently put PE prices into the
    # ce_iv slot and would have produced a plausible-looking wrong answer.
    return (part('CE', 'ltp'), part('PE', 'ltp'),
            part('CE', 'iv'), part('PE', 'iv'), expiry, und)


def atm_series(ce_ltp: pd.DataFrame, pe_ltp: pd.DataFrame):
    """ATM strike, forward price and straddle price, per minute.

    ATM by minimum |C - P|: put-call parity makes that difference zero exactly at
    the forward, so it locates ATM without needing a spot feed at all. The same
    identity gives the forward, F = K + C - P, taken as the median across strikes
    (any single strike is noisy when one leg is stale).
    """
    common = ce_ltp.columns.intersection(pe_ltp.columns)
    if len(common) < 3:
        return None
    ce, pe = ce_ltp[common], pe_ltp[common]
    both = ce.notna() & pe.notna()
    cp = (ce - pe).where(both)

    fwd = cp.add(pd.Series(np.asarray(common, float), index=common), axis=1).median(axis=1)
    atm = cp.abs().idxmin(axis=1)

    kpos = {k: i for i, k in enumerate(common)}
    rows = np.arange(len(ce))
    col = atm.map(kpos)
    ok = col.notna().values
    ci = np.where(ok, col.fillna(0).values.astype(int), 0)
    strad = np.full(len(ce), np.nan)
    strad[ok] = (ce.values[rows, ci] + pe.values[rows, ci])[ok]
    return pd.DataFrame({'atm': atm, 'fwd': fwd, 'straddle': strad}, index=ce.index)


def short_straddle_charges(open_prem: float, close_prem: float, qty: int) -> float:
    """Round-trip charges for a SHORT straddle: 4 orders, STT on the OPENING sell.

    A local model rather than signal_pnl.charges() on purpose, and the reason is not
    convenience: that function assumes a LONG round trip, so it applies the 0.0625%
    STT to the closing price. For a short the taxable sell is the OPENING leg, and at
    these premiums the two differ by a few rupees per lot in whichever direction the
    trade went -- i.e. it would quietly flatter losers and penalise winners, which is
    exactly the bias this whole script exists to avoid. Rates are the same ones
    documented in signal_pnl.charges(); if those change, change both.
    """
    brokerage = 20.0 * 4                                    # 2 legs, in and out
    stt = 0.000625 * open_prem * qty                        # sell side only
    txn = 0.0005 * (open_prem + close_prem) * qty
    stamp = 0.00003 * close_prem * qty                      # buy side
    gst = 0.18 * (brokerage + txn)
    return brokerage + stt + txn + stamp + gst


def realised_vol(fwd: pd.Series, i0: int, i1: int, year_min: int) -> float:
    """Annualised close-to-close vol of the forward over bars [i0, i1)."""
    seg = fwd.iloc[i0:i1].dropna()
    if len(seg) < 20:
        return np.nan
    r = np.diff(np.log(seg.values.astype(float)))
    r = r[np.isfinite(r)]
    if len(r) < 15:
        return np.nan
    return float(np.std(r, ddof=1) * np.sqrt(year_min) * 100.0)


def dte_of(expiry: str, date_str: str):
    for fmt in ('%d %b %y', '%d %B %y', '%d %b %Y'):
        try:
            e = datetime.strptime(expiry.strip().title(), fmt).date()
            return (e - datetime.strptime(date_str, '%Y%m%d').date()).days
        except ValueError:
            continue
    return None


def collect(dates, holds, step, segment, loader=None):
    loader = loader or load_day
    rows = []
    for date_str in dates:
        df = loader(date_str, segment)
        if df is None or df.empty:
            print(f'{date_str}: no {segment} data')
            continue
        built = build_chain(df)
        if built is None:
            print(f'{date_str}: no option measurements parsed')
            continue
        ce_ltp, pe_ltp, ce_iv, pe_iv, expiry, und = built
        a = atm_series(ce_ltp, pe_ltp)
        if a is None or a['straddle'].notna().sum() < 60:
            print(f'{date_str}: chain too thin ({len(ce_ltp.columns)} CE strikes)')
            continue

        iv_atm = pd.Series(np.nan, index=a.index)
        if not ce_iv.empty and not pe_iv.empty:
            common = ce_iv.columns.intersection(pe_iv.columns)
            if len(common):
                kpos = {k: i for i, k in enumerate(common)}
                col = a['atm'].map(kpos)
                ok = col.notna().values
                ci = np.where(ok, col.fillna(0).values.astype(int), 0)
                r = np.arange(len(a))
                v = np.nanmean(np.stack([ce_iv[common].values[r, ci],
                                         pe_iv[common].values[r, ci]]), axis=0)
                iv_atm = pd.Series(np.where(ok, v, np.nan), index=a.index)

        dte = dte_of(expiry[0], date_str) if expiry else None
        lot = LOT.get(und[0] if und else '', 20)
        n = len(a)
        print(f'{date_str}: {len(ce_ltp.columns)} CE / {len(pe_ltp.columns)} PE '
              f'strikes, {n} minutes, expiry {expiry[0] if expiry else "?"} '
              f'(DTE {dte}), lot {lot}, iv {"present" if iv_atm.notna().any() else "MISSING"}')

        # THE STRIKE IS FIXED AT ENTRY. a['straddle'] is the ATM straddle minute by
        # minute, i.e. a different strike each minute as spot moves — comparing it at
        # t and t+H prices two different instruments and silently cancels the entire
        # gamma loss, because the strike chases the underlying. Measured on synthetic
        # chains it made a world with realised vol 3x implied look PROFITABLE.
        # A short straddle holds the strike it sold.
        common = ce_ltp.columns.intersection(pe_ltp.columns)
        strad = ce_ltp[common] + pe_ltp[common]

        for H in holds:
            for i in range(0, n - H, step):
                K = a['atm'].iloc[i]
                if not np.isfinite(K) or K not in strad.columns:
                    continue
                s0, s1 = strad[K].iloc[i], strad[K].iloc[i + H]
                if not (np.isfinite(s0) and np.isfinite(s1)) or s0 <= 0:
                    continue
                rows.append({
                    'date': date_str, 'dte': dte, 'H': H, 'i': i, 'lot': lot,
                    't': a.index[i].strftime('%H:%M'), 'K': float(K),
                    'straddle0': s0, 'straddle1': s1,
                    'move_pts': float(abs(a['fwd'].iloc[i + H] - a['fwd'].iloc[i])),
                    'decay_pct': 100.0 * (s0 - s1) / s0,
                    'gross': (s0 - s1) * lot,
                    'chg': short_straddle_charges(s0, s1, lot),
                    'iv': float(iv_atm.iloc[i]) if np.isfinite(iv_atm.iloc[i]) else np.nan,
                    'rv_trade': realised_vol(a['fwd'], i, i + H, YEAR_MIN_TRADING),
                    'rv_cal': realised_vol(a['fwd'], i, i + H, YEAR_MIN_CALENDAR),
                })
    return pd.DataFrame(rows)


def independent(sub: pd.DataFrame, H: int, step: int) -> pd.DataFrame:
    """One window per H minutes per day — the only rows that are real observations."""
    keep = []
    for _, g in sub.groupby('date'):
        last = -10 ** 9
        for _, r in g.sort_values('i').iterrows():
            if r['i'] - last >= H:
                keep.append(r)
                last = r['i']
    return pd.DataFrame(keep)


def report(df: pd.DataFrame, holds, step) -> None:
    print('\n' + '=' * 78)
    print('1. SHORT ATM STRADDLE, SOLD AT t AND CLOSED AT t+H   (the actual trade)')
    print('=' * 78)
    print(f"  {'hold':>6}{'indep':>7}{'overlap':>9}{'net/wk':>10}{'median':>9}"
          f"{'win%':>7}{'worst':>10}{'p5':>9}{'chg%':>7}")
    print('  ' + '-' * 72)
    for H in holds:
        sub = df[df['H'] == H]
        if sub.empty:
            continue
        ind = independent(sub, H, step)
        if len(ind) < 3:
            print(f'  {H:>5}m{len(ind):>7}{len(sub):>9}   too few independent windows')
            continue
        net = (ind['gross'] - ind['chg']).values
        worst5 = np.percentile(net, 5)
        print(f'  {H:>5}m{len(ind):>7}{len(sub):>9}{net.mean():>10,.0f}'
              f'{np.median(net):>9,.0f}{100 * (net > 0).mean():>7.0f}'
              f'{net.min():>10,.0f}{worst5:>9,.0f}'
              f'{100 * ind["chg"].sum() / max(abs(ind["gross"].sum()), 1e-9):>7.0f}')
    print('  net/wk = mean NET rupees per independent window, one lot, after charges.')
    print('  Read `worst` and `p5` FIRST: short vol is paid to absorb those.')

    print('\n  tail concentration — how much of the result is a handful of windows:')
    for H in holds:
        ind = independent(df[df['H'] == H], H, step)
        if len(ind) < 8:
            continue
        net = np.sort((ind['gross'] - ind['chg']).values)
        tot = net.sum()
        k = max(1, len(net) // 20)
        print(f'    {H:>4}m  total {tot:>9,.0f}   worst {k} of {len(net)} '
              f'= {net[:k].sum():>9,.0f}   '
              f'({"DOMINATED by the tail" if tot != 0 and abs(net[:k].sum()) > abs(tot) else "tail not dominant"})')

    if df['iv'].notna().any():
        print('\n' + '=' * 78)
        print('2. DIAGNOSTIC: implied vs realised, under BOTH annualisations')
        print('=' * 78)
        scale = 100.0 if df['iv'].median() < 1.0 else 1.0
        if scale != 1.0:
            print('  iv looked like a fraction, not a percent — scaled by 100.')
        print(f"  {'hold':>6}{'n':>6}{'IV':>9}{'RV trade-time':>15}{'IV/RV':>8}"
              f"{'RV cal-time':>13}{'IV/RV':>8}")
        print('  ' + '-' * 66)
        for H in holds:
            ind = independent(df[df['H'] == H], H, step)
            if len(ind) < 3 or ind['iv'].notna().sum() < 3:
                continue
            iv = float(np.nanmedian(ind['iv'])) * scale
            rt = float(np.nanmedian(ind['rv_trade']))
            rc = float(np.nanmedian(ind['rv_cal']))
            print(f'  {H:>5}m{len(ind):>6}{iv:>9.2f}{rt:>15.2f}{iv / rt:>8.2f}'
                  f'{rc:>13.2f}{iv / rc:>8.2f}')
        print('  IV/RV > 1 means implied is rich, i.e. a premium to selling.')
        print('  If the two IV/RV columns straddle 1.0, the diagnostic is telling you')
        print('  nothing except which convention you chose — trust section 1 instead.')
    else:
        print('\n  NOTE: no `iv` in the data, so section 2 is skipped. The straddle')
        print('  test above does not need it and is the answer either way.')
        print('  To collect it: SUBSCRIBE_MODE=option_greeks in viz_hedge/.env')

    if df['dte'].notna().any() and df['dte'].nunique() > 1:
        print('\n' + '=' * 78)
        print('3. BY DAYS TO EXPIRY   (theta and gamma both explode near expiry —')
        print('   pooling these averages two unrelated regimes)')
        print('=' * 78)
        print(f"  {'DTE':>5}{'hold':>7}{'days':>6}{'indep':>7}{'net/wk':>10}"
              f"{'median':>9}{'win%':>7}{'worst':>10}")
        print('  ' + '-' * 61)
        thin_dte = False
        for d in sorted(df['dte'].dropna().unique()):
            for H in holds:
                sub = df[(df['H'] == H) & (df['dte'] == d)]
                ind = independent(sub, H, step)
                if len(ind) < 3:
                    continue
                ndays = sub['date'].nunique()
                thin_dte = thin_dte or ndays < 3
                net = (ind['gross'] - ind['chg']).values
                print(f'  {int(d):>5}{H:>6}m{ndays:>6}{len(ind):>7}{net.mean():>10,.0f}'
                      f'{np.median(net):>9,.0f}{100 * (net > 0).mean():>7.0f}'
                      f'{net.min():>10,.0f}')
        if thin_dte:
            print('\n  *** THE `days` COLUMN IS THE ONE THAT MATTERS HERE. ***')
            print('  A DTE bucket built from ONE session is not a DTE effect, it is')
            print('  that day. With a 375-minute session and a 60-minute hold you get')
            print('  ~6 "independent" windows out of a single day — they share the same')
            print('  open, the same news and the same regime, so the row is n=1 dressed')
            print('  up as n=6. Do not change a DTE rule on any row showing days < 3.')

    print('\n' + '=' * 78)
    print('HOW MANY INDEPENDENT WINDOWS WOULD YOU NEED?')
    print('=' * 78)
    for H in holds:
        ind = independent(df[df['H'] == H], H, step)
        if len(ind) < 3:
            continue
        net = (ind['gross'] - ind['chg']).values
        sd = net.std(ddof=1)
        if sd > 0 and abs(net.mean()) > 0:
            need = (2.0 * sd / abs(net.mean())) ** 2
            print(f'  {H:>4}m  mean {net.mean():>8,.0f}  sd {sd:>8,.0f}  ->  ~{need:,.0f} '
                  f'windows to separate this mean from zero at 2 s.e.')
            print(f'         you have {len(ind)}. '
                  f'{"ENOUGH" if len(ind) >= need else "NOT ENOUGH — this result is noise so far"}')
    return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', required=True, help='YYYYMMDD, comma-separated')
    ap.add_argument('--hold', default='60,120,180', help='minutes held')
    ap.add_argument('--step', type=int, default=5, help='sampling interval, minutes')
    ap.add_argument('--segment', default='BSE_FO')
    a = ap.parse_args()

    dates = [d.strip() for d in a.date.split(',') if d.strip()]
    holds = [int(h) for h in a.hold.split(',')]
    df = collect(dates, holds, a.step, a.segment)
    if df.empty:
        print('\nno usable windows')
        return 1
    report(df, holds, a.step)
    print('\nThis measures the trade, not a recommendation to take it. Short vol is')
    print('paid for absorbing the tail; a few sessions cannot show you the tail.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
