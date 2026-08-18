#!/usr/bin/env python3
"""Does implied volatility exceed subsequent realised volatility in Indian markets?

    python utils/vix_premium.py --years 2

THE QUESTION THIS ANSWERS, AND WHY IT COMES FIRST
-------------------------------------------------
Every short-volatility structure — iron condor, short straddle, credit spread — is
the same bet: that implied vol is priced above the vol that actually shows up. The
structure only decides the payoff shape and the margin. So before designing one,
measure whether the premium exists.

India VIX is the market's 30-day annualised implied vol for NIFTY. NIFTY's realised
vol over the FOLLOWING period is what a seller actually pays out against. The gap
between them is the variance risk premium, and it is the entire economic case for
selling options.

Both instruments are live indices with long history and no expiry, so this needs no
option chain, no expired-contract lookups, and no waiting for a trade to mature.
That is the point: it answers in one run what a live trade answers in one sample.

WHY NOT JUST RUN THE TRADE FOR A WEEK
-------------------------------------
A short straddle held to expiry wins roughly three times in four. Measured on
synthetic chains in vol_premium.py, a straddle with EXACTLY ZERO expectancy by
construction still showed a median window of +Rs 61 against a mean of -Rs 99. One
trade samples the median, not the mean, so the single most likely outcome of a
one-week live test is a profit that means nothing — and that is the result most
likely to be believed.

THE TRAPS, AGAIN
----------------
1. OVERLAPPING WINDOWS. Sampling daily with a 21-day forward window gives windows
   sharing 20 of 21 days. A year of daily data is ~250 rows but only ~12 INDEPENDENT
   21-day windows. Every headline below is computed on non-overlapping windows, with
   the overlapping count shown alongside so the difference stays visible. Quoting 250
   here would repeat the "30 series" error that turned out to be 5 sessions.

2. HORIZON MISMATCH. VIX is a 30-DAY implied measure. Comparing it against 5-day
   realised vol is informative about the horizon you actually trade, but it is not a
   like-for-like comparison unless the vol term structure is flat. The 21-day row is
   the clean test; shorter rows are indicative. Both are printed, labelled.

3. THE TAIL IS THE WHOLE RISK. The variance premium is positive most of the time and
   sharply negative in crises — that is what it is compensation FOR. A mean without
   the worst-case column is a recruitment poster, not a measurement.

ANNUALISATION IS UNAMBIGUOUS HERE, unlike the intraday case: VIX is quoted as an
annualised percentage and realised vol from daily returns conventionally uses
sqrt(252). No convention choice changes the sign.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from math import exp, lgamma, sqrt
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                             # noqa: E402,F401

from poll_ohlc import load_token                             # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
HIST_URL = 'https://api.upstox.com/v3/historical-candle'
VIX_KEY = 'NSE_INDEX|India VIX'
NIFTY_KEY = 'NSE_INDEX|Nifty 50'
TRADING_DAYS = 252


def fetch_daily(key: str, d_from, d_to, token: str, session=None) -> pd.Series:
    """Daily closes -> Series indexed by date. Response is
    data.candles = [[iso_ts, o, h, l, c, volume, oi], ...], newest first."""
    url = (f'{HIST_URL}/{quote(key, safe="")}/days/1/'
           f'{d_to:%Y-%m-%d}/{d_from:%Y-%m-%d}')
    http = session or requests
    r = http.get(url, headers={'Accept': 'application/json',
                               'Authorization': f'Bearer {token}'}, timeout=60)
    if r.status_code == 401:
        raise SystemExit('Upstox returned 401 — access token expired or missing.')
    if r.status_code >= 400:
        raise SystemExit(f'{key}: HTTP {r.status_code} {str(r.text)[:200]}\n'
                         f'If the instrument key is wrong, pass --vix-key / '
                         f'--index-key explicitly.')
    body = r.json()
    if body.get('status') != 'success':
        raise SystemExit(f'{key}: status={body.get("status")} {str(body)[:200]}')
    rows = {}
    for c in ((body.get('data') or {}).get('candles') or []):
        if len(c) < 5:
            continue
        rows[datetime.fromisoformat(str(c[0])).astimezone(IST).date()] = float(c[4])
    return pd.Series(rows).sort_index()


def _c4(n: int) -> float:
    """E[s]/sigma for a sample of n normal observations.

    The sample standard deviation is a BIASED estimator of sigma, low by ~6% at
    n=5, ~2.7% at n=10, ~1.2% at n=21. Uncorrected, that understates realised vol,
    and since the premium here is VIX MINUS realised it would manufacture premium
    that does not exist — largest at the short horizons, which are exactly the ones
    a multi-day trader cares about. On a 15% vol, n=5 invents ~0.9 vol points.
    """
    if n < 2:
        return 1.0
    return sqrt(2.0 / (n - 1)) * exp(lgamma(n / 2.0) - lgamma((n - 1) / 2.0))


def forward_rv(px: pd.Series, n: int) -> pd.Series:
    """Annualised realised vol of the NEXT n trading days, in percent.

    Strictly forward: the value at t uses returns from t+1..t+n only, so it is never
    knowable at t. That is deliberate — we are measuring what a seller pays out
    against, not building a signal. Verified by construction in the tests: injecting
    volatility before t leaves rv[t] bit-identical, injecting it after t moves it.
    """
    r = np.log(px / px.shift(1))
    # shift(-1) so the window starts the day AFTER t; shift(-(n-1)) realigns the
    # rolling result (which lands on the window's last row) back onto t
    fwd = r.shift(-1).rolling(n).std(ddof=1).shift(-(n - 1))
    return fwd / _c4(n) * np.sqrt(TRADING_DAYS) * 100.0


def independent_rows(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Every n-th row: non-overlapping forward windows."""
    return df.iloc[::n]


def straddle_rupees(prem_vol_pts: float, spot: float, n: int, lot: int) -> float:
    """Indicative rupees per lot from selling `prem_vol_pts` of vol for n days.

    ATM straddle ~ 0.8 * S * sigma * sqrt(T), so the edge is ~0.8 * S * dSigma *
    sqrt(n/252) * lot. APPROXIMATE: it assumes a delta-hedged position and ignores
    the smile, discrete hedging and costs. Use it for order of magnitude only —
    vol_premium.py measures the real thing against actual option prices.
    """
    return 0.8 * spot * (prem_vol_pts / 100.0) * np.sqrt(n / TRADING_DAYS) * lot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--years', type=float, default=2.0)
    ap.add_argument('--horizons', default='5,10,21',
                    help='trading days forward (21 ~ VIX\'s own 30-day horizon)')
    ap.add_argument('--vix-key', default=VIX_KEY)
    ap.add_argument('--index-key', default=NIFTY_KEY)
    ap.add_argument('--lot', type=int, default=75, help='NIFTY lot, for the '
                                                        'indicative rupee column')
    a = ap.parse_args()

    horizons = [int(h) for h in a.horizons.split(',')]
    token = load_token()
    d_to = datetime.now(IST).date()
    d_from = d_to - timedelta(days=int(365 * a.years) + 10)
    sess = requests.Session()

    vix = fetch_daily(a.vix_key, d_from, d_to, token, sess)
    idx = fetch_daily(a.index_key, d_from, d_to, token, sess)
    if vix.empty or idx.empty:
        print('no data returned — check the instrument keys')
        return 1

    df = pd.DataFrame({'vix': vix, 'spot': idx}).dropna()
    print(f'{a.vix_key}: {len(vix)} days   {a.index_key}: {len(idx)} days')
    print(f'overlapping range: {df.index[0]} .. {df.index[-1]}  ({len(df)} sessions)')
    if len(df) < 120:
        print('\nWARNING: fewer sessions than expected. Daily history is retained for')
        print('about a year; if this looks short, that is the API limit, not a bug.')

    print('\n' + '=' * 78)
    print('VARIANCE RISK PREMIUM = VIX(t) - realised vol of NIFTY over the NEXT n days')
    print('=' * 78)
    print(f"  {'n':>4}{'indep':>7}{'overlap':>9}{'mean':>9}{'median':>9}"
          f"{'sd':>8}{'t':>6}{'IV>RV %':>9}{'worst':>9}{'p5':>8}{'~Rs/lot':>10}")
    print('  ' + '-' * 88)

    results, tstats = {}, {}
    for n in horizons:
        d = df.copy()
        d['rv'] = forward_rv(d['spot'], n)
        d['prem'] = d['vix'] - d['rv']
        d = d.dropna(subset=['prem'])
        if len(d) < n * 3:
            print(f'  {n:>4}   too few rows ({len(d)}) for {n}-day windows')
            continue
        ind = independent_rows(d, n)
        p = ind['prem'].values
        # t on the INDEPENDENT windows only. Computing it on the 481 overlapping
        # rows would divide by sqrt(481) instead of sqrt(23) and inflate t by ~4.5x,
        # turning a marginal result into a certainty. This is the same overlap error
        # that made "30 series" look like 30 sessions.
        sd = float(np.std(p, ddof=1))
        se = sd / np.sqrt(len(p)) if len(p) > 1 else np.nan
        t = float(np.mean(p) / se) if se and se > 0 else np.nan
        tstats[n] = (t, sd, len(p))
        rup = straddle_rupees(float(np.mean(p)), float(ind['spot'].mean()), n, a.lot)
        results[n] = ind
        print(f'  {n:>4}{len(ind):>7}{len(d):>9}{np.mean(p):>9.2f}'
              f'{np.median(p):>9.2f}{sd:>8.2f}{t:>6.1f}'
              f'{100 * (p > 0).mean():>9.0f}'
              f'{np.min(p):>9.2f}{np.percentile(p, 5):>8.2f}{rup:>10,.0f}')

    print('  units are ANNUALISED VOL POINTS. positive = implied richer than realised.')
    print('  t is computed on INDEPENDENT windows. |t| < 2 means the mean is not')
    print('  distinguishable from zero however pleasant it looks.')
    for n, (t, sd, k) in tstats.items():
        if np.isfinite(t) and abs(t) < 2.0:
            need = int(np.ceil((2.0 * sd / abs(np.mean(results[n]['prem'].values))) ** 2))
            print(f'    n={n}: t={t:.1f} on {k} windows — NOT significant; '
                  f'~{need} windows needed (~{need * n / 252:.1f} years)')
    print('  n=21 is the like-for-like row (VIX is a 30-calendar-day measure);')
    print('  shorter rows tell you about YOUR holding period but mix horizons.')
    print('  ~Rs/lot is indicative only — see straddle_rupees() for what it assumes.')

    for n, ind in results.items():
        p = np.sort(ind['prem'].values)
        k = max(1, len(p) // 20)
        print(f'\n  n={n}: worst {k} of {len(p)} windows sum {p[:k].sum():>8.1f} vol '
              f'pts against a total of {p.sum():>8.1f}')
        if p.sum() != 0 and abs(p[:k].sum()) > 0.5 * abs(p.sum()):
            print('        the tail dominates — the average is not what you would '
                  'have experienced')

    # Regime split: the premium is usually fat when VIX is low and violently negative
    # when vol spikes. A single average hides both.
    # SHORTEST horizon, not the longest: it has the most independent windows. At
    # n=21 there are only ~23 windows, so quartiles hold 5-8 each and the buckets
    # will disagree with each other purely by chance. At n=5 they hold ~25.
    n = min(results) if results else None
    if n:
        ind = results[n].copy()
        nb = min(4, max(2, len(ind) // 12))
        ind['bucket'] = pd.qcut(ind['vix'], nb, duplicates='drop')
        print(f'\n  BY VIX LEVEL AT ENTRY (n={n}, the horizon with the most windows)')
        print(f'  — is the premium there when you would actually sell?')
        print(f"    {'VIX range':>18}{'windows':>9}{'mean prem':>11}{'IV>RV %':>9}"
              f"{'worst':>9}")
        print('    ' + '-' * 54)
        thin = False
        for b, g in ind.groupby('bucket', observed=True):
            v = g['prem'].values
            thin = thin or len(g) < 15
            print(f'    {str(b):>18}{len(g):>9}{v.mean():>11.2f}'
                  f'{100 * (v > 0).mean():>9.0f}{v.min():>9.2f}')
        if thin:
            print('    AT LEAST ONE BUCKET HAS UNDER 15 WINDOWS. Buckets that small')
            print('    disagree with each other by chance; a negative bucket here is')
            print('    not evidence of anything. Do not build a VIX filter on this.')

    print('\n' + '=' * 78)
    print('HOW TO READ THIS')
    print('=' * 78)
    print('  A positive mean at n=21 says the premium exists. It does NOT say the')
    print('  trade is safe: you are being PAID to absorb the worst column, and the')
    print('  worst column is what a thin account cannot survive.')
    print('  A premium that only appears in the low-VIX buckets means the edge is')
    print('  there precisely when the payout is smallest, which is the usual shape.')
    print('  Costs are not in these numbers. vol_premium.py prices the actual trade.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
