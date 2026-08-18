#!/usr/bin/env python3
"""Does the angle signal predict ANYTHING? Forward returns on every raw signal.

    python utils/signal_forward.py --date 20260817,20260818

WHY THIS EXISTS, AND WHY IT BEATS replay_journal.py
---------------------------------------------------
replay_journal scores CLOSED TRADES. On 17-18 August that is 57 observations, and
each one has been through four filters that have nothing to do with the detector:
MAX_POSITIONS_PER_UNDERLYING=1 (which suppressed 303 of 851 signals), the daily loss
limit, MIN_PREMIUM, and the exit logic (4-sigma stop / 2-sigma trail / 20-min cap).
A negative result there could be the detector, the exits, or the cap, and the tables
cannot tell them apart. It has already produced one confident answer that evaporated:
the ratio>=1.50 filter looked positive on 12 and 13 August and inverted on 18.

This scores the SIGNAL ITSELF -- all of them, filled or suppressed -- by asking one
question: after the signal fires, does the option go up more than it would have at a
random moment? No position sizing, no exits, no cap. If the answer is no, no amount
of exit tuning will save it, and that is worth knowing before spending another week
on exits.

THE CONTROL IS THE WHOLE POINT
------------------------------
Raw forward returns are meaningless here. These are long option positions 2-3 days
from expiry: theta alone makes the average forward return negative at every horizon,
for every entry, signal or not. So "signals lose money over 10 minutes" would be true
of a coin flip and proves nothing.

Two controls, drawn from the SAME instrument on the SAME day, and carrying the same
date/side labels as the signals so every table below compares like with like:
  rand  -- a uniformly random tick in the session. Controls for the instrument and
           for theta drift.
  near  -- a random tick within +/-NEAR_MIN minutes of a real signal. Also controls
           for TIME OF DAY, which matters because the one robust finding in this
           project is that large moves cluster at the session edges (18/18 series).
           An identical study on order-flow fields found a pure clock control scoring
           AUC 0.707 against 0.577 for volume. `near` is the honest benchmark: if the
           signal beats `rand` but not `near`, the signal is a clock.

The p column is the signal mean's percentile inside a bootstrap of `near` means at
the same n. 50 means the signal sits exactly where chance puts it. Read it first.

BREAKEVEN, so a positive number is not mistaken for a profitable one
-------------------------------------------------------------------
Round-trip charges measured on 17 August were Rs 1,881 against Rs 228,647 of entry
notional, i.e. the option must gain 0.82% before the trade clears costs. That line is
printed under every table. An edge of +0.2% can be real and still lose money.
"""

import argparse
import json
import sys
from collections import defaultdict
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
NS_MIN = 60 * 10 ** 9
HORIZONS = (2, 5, 10, 20)
MAX_STALE_MIN = 2          # an asof price older than this is a data gap, not a price
NEAR_MIN = 20              # +/- window for the time-matched control
N_CTRL = 5                 # draws per signal, per control type
N_BOOT = 2000
# Measured, not assumed: 17 Aug was Rs 1,881 of charges on Rs 228,647 of entry
# notional across 31 trades. Do not round this down -- it is the hurdle.
BREAKEVEN_PCT = 0.82


def read_jsonl(p: Path) -> list:
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


def measurement_for(rec: dict) -> str:
    """BSE_FO|12345 + 'SENSEX 77400 PE 20 AUG 26' -> 'BSE_SENSEX 77400 PE 20 AUG 26'.

    Same construction as trade_conditions.py — the exchange prefix is the part of the
    instrument key BEFORE the first underscore, not the whole segment.
    """
    exch = (rec.get('instrument_key') or '').split('|')[0].split('_')[0]
    return f"{exch}_{rec.get('symbol', '')}"


def side_of(symbol: str) -> str:
    s = f' {symbol} '
    return 'CE' if ' CE ' in s else ('PE' if ' PE ' in s else '?')


def load_ltp(measurement: str, date_str: str) -> pd.Series:
    d = datetime.strptime(date_str, '%Y%m%d').date()
    q = (f'from(bucket: "{settings.INFLUX_BUCKET}")\n'
         f'  |> range(start: {d}T00:00:00+05:30, '
         f'stop: {d + timedelta(days=1)}T00:00:00+05:30)\n'
         f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
         f'  |> filter(fn: (r) => r._field == "ltp")\n'
         f'  |> keep(columns: ["_time", "_value"])')
    with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                        org=settings.INFLUX_ORG, timeout=300_000) as c:
        df = c.query_api().query_data_frame(q)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else None
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    s = pd.Series(df['_value'].astype(float).values,
                  index=pd.to_datetime(df['_time']))
    return s.sort_index()


def epoch_ns(index) -> np.ndarray:
    """int64 nanoseconds for a DatetimeIndex, tz-aware or not.

    Not `.view('int64')` (deprecated on Index in pandas 2.2, removed later) and not a
    bare `.astype('int64')` (raises on tz-aware in some 1.x builds). Dropping the tz
    first makes both paths identical, and the values are UTC either way.
    """
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert('UTC').tz_localize(None)
    return idx.to_numpy(dtype='datetime64[ns]').astype('int64')


def _asof(idx: np.ndarray, vals: np.ndarray, t: int, max_stale: int) -> float:
    """Last price at or before t. NaN if the nearest tick is older than max_stale —
    carrying a 10-minute-old price forward would invent a 0% return across a gap."""
    i = np.searchsorted(idx, t, side='right') - 1
    if i < 0 or (t - idx[i]) > max_stale:
        return np.nan
    return float(vals[i])


def fwd(idx: np.ndarray, vals: np.ndarray, t: int, horizons) -> dict:
    max_stale = MAX_STALE_MIN * NS_MIN
    p0 = _asof(idx, vals, t, max_stale)
    out = {}
    for h in horizons:
        th = t + h * NS_MIN
        # Beyond the last tick the asof would return the closing price for every
        # horizon, manufacturing a flat tail at the end of each session.
        p1 = _asof(idx, vals, th, max_stale) if th <= idx[-1] else np.nan
        out[h] = (100.0 * (p1 / p0 - 1.0)
                  if (np.isfinite(p0) and p0 > 0 and np.isfinite(p1)) else np.nan)
    return out


def boot_pctile(signal_mean: float, pool: np.ndarray, n: int, rng) -> float:
    """Where the signal mean sits in the distribution of control means at the same n."""
    pool = np.asarray(pool, float)
    pool = pool[np.isfinite(pool)]
    if not np.isfinite(signal_mean) or len(pool) < 30 or n < 3:
        return np.nan
    draws = rng.choice(pool, size=(N_BOOT, n), replace=True).mean(axis=1)
    return float(100.0 * (draws < signal_mean).mean())


def collect(dates, horizons, near_min, rng, loader=None):
    """-> (signals df, controls df). Controls carry date/side so every table below
    compares signals against controls from the same day and the same option side.

    `loader` is resolved here rather than defaulted to load_ltp in the signature, so
    a test can substitute a synthetic price series by patching the module attribute.
    """
    loader = loader or load_ltp
    sig_rows, ctrl_rows, dropped = [], [], 0

    for date_str in dates:
        jdir = Path(settings.JOURNAL_DIR) / date_str
        sigs = [s for s in read_jsonl(jdir / 'signals.jsonl')
                if str(s.get('action', '')).startswith('ENTER')]
        if not sigs:
            print(f'{date_str}: no ENTER signals in {jdir}')
            continue

        by_meas = defaultdict(list)
        for s in sigs:
            by_meas[measurement_for(s)].append(s)
        print(f'{date_str}: {len(sigs)} ENTER signals across {len(by_meas)} instruments')

        for meas, rows in by_meas.items():
            series = loader(meas, date_str)
            if len(series) < 200:
                dropped += len(rows)
                print(f'    {meas}: only {len(series)} ticks — skipped')
                continue
            idx, vals = epoch_ns(series.index), series.values.astype(float)

            for s in rows:
                t = int(pd.Timestamp(datetime.fromisoformat(s['ts'])
                                     .astimezone(timezone.utc)).value)
                f = fwd(idx, vals, t, horizons)
                if not any(np.isfinite(v) for v in f.values()):
                    dropped += 1
                    continue
                m = s.get('meta') or {}
                ang, thr = m.get('angle_deg'), m.get('threshold_deg')
                side = side_of(str(s.get('symbol', '')))
                sig_rows.append({'date': date_str, 'side': side,
                                 'ratio': (float(ang) / float(thr))
                                          if (ang and thr) else np.nan,
                                 **{f'h{h}': f[h] for h in horizons}})

                for _ in range(N_CTRL):
                    j = int(rng.integers(0, len(idx)))
                    cf = fwd(idx, vals, int(idx[j]), horizons)
                    ctrl_rows.append({'date': date_str, 'side': side, 'kind': 'rand',
                                      **{f'h{h}': cf[h] for h in horizons}})

                    off = int(rng.integers(-near_min, near_min + 1)) * NS_MIN
                    cf = fwd(idx, vals, t + off, horizons)
                    ctrl_rows.append({'date': date_str, 'side': side, 'kind': 'near',
                                      **{f'h{h}': cf[h] for h in horizons}})

    return (pd.DataFrame(sig_rows), pd.DataFrame(ctrl_rows), dropped)


def build(sub_sig: pd.DataFrame, sub_ctrl: pd.DataFrame, horizons, rng) -> list:
    rnd = sub_ctrl[sub_ctrl['kind'] == 'rand'] if len(sub_ctrl) else sub_ctrl
    nr = sub_ctrl[sub_ctrl['kind'] == 'near'] if len(sub_ctrl) else sub_ctrl
    out = []
    for h in horizons:
        col = f'h{h}'
        v = sub_sig[col].to_numpy(dtype=float)
        v = v[np.isfinite(v)]
        if len(v) < 3:
            continue
        npool = nr[col].to_numpy(dtype=float) if len(nr) else np.array([])
        rpool = rnd[col].to_numpy(dtype=float) if len(rnd) else np.array([])
        nm = float(np.nanmean(npool)) if np.isfinite(npool).any() else np.nan
        rm = float(np.nanmean(rpool)) if np.isfinite(rpool).any() else np.nan
        out.append({'h': h, 'n': len(v), 'mean': float(v.mean()),
                    'med': float(np.median(v)), 'win': 100.0 * float((v > 0).mean()),
                    'rand': rm, 'near': nm, 'edge': float(v.mean()) - nm,
                    'p': boot_pctile(float(v.mean()), npool, len(v), rng)})
    return out


def _f(x, w, p=3):
    return f'{"":>{w}}' if x is None or not np.isfinite(x) else f'{x:>{w}.{p}f}'


def table(title: str, rows: list) -> None:
    print(f'\n── {title}')
    if not rows:
        print('  (too few observations)')
        return
    print(f"  {'horizon':>8}{'n':>6}{'sig mean':>10}{'sig med':>9}{'win%':>7}"
          f"{'rand':>9}{'near':>9}{'edge':>9}{'p':>6}")
    print('  ' + '-' * 73)
    for r in rows:
        edge = ('' if not np.isfinite(r['edge']) else f"{r['edge']:>+9.3f}")
        print(f"  {r['h']:>7}m{r['n']:>6}{_f(r['mean'], 10)}{_f(r['med'], 9)}"
              f"{_f(r['win'], 7, 0)}{_f(r['rand'], 9)}{_f(r['near'], 9)}"
              f"{edge:>9}{_f(r['p'], 6, 0)}")
    print(f'  breakeven after charges: +{BREAKEVEN_PCT:.2f}%  '
          f'(edge must clear this, not zero)')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', required=True, help='YYYYMMDD, comma-separated for many')
    ap.add_argument('--horizons', default=','.join(str(h) for h in HORIZONS))
    ap.add_argument('--near', type=int, default=NEAR_MIN)
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args()

    dates = [d.strip() for d in a.date.split(',') if d.strip()]
    horizons = [int(h) for h in a.horizons.split(',')]
    rng = np.random.default_rng(a.seed)

    sig, ctrl, dropped = collect(dates, horizons, a.near, rng)
    if sig.empty:
        print('no usable signals')
        return 1

    print(f'\n{len(sig)} signals scored, {dropped} dropped '
          f'(data gap at the signal, or the whole horizon past the close)')
    print(f'controls: {len(ctrl[ctrl["kind"] == "rand"]):,} random / '
          f'{len(ctrl[ctrl["kind"] == "near"]):,} time-matched draws')

    table('ALL SIGNALS', build(sig, ctrl, horizons, rng))

    for d in sorted(sig['date'].unique()):
        table(f'DAY {d}', build(sig[sig['date'] == d],
                                ctrl[ctrl['date'] == d], horizons, rng))

    for s in ('CE', 'PE'):
        sub = sig[sig['side'] == s]
        if len(sub) >= 20:
            table(f'SIDE {s}', build(sub, ctrl[ctrl['side'] == s], horizons, rng))

    # Does a STRONGER signal predict better? replay_journal said yes on 17 Aug and no
    # on 18 Aug, off 15 and 21 trades. Here each bucket holds hundreds.
    print('\n── BY SIGNAL STRENGTH (ratio = angle / adaptive threshold)')
    hdr = ''.join(f'{f"h{h}m":>10}' for h in horizons)
    print(f"  {'bucket':>12}{'n':>7}{hdr}{'win h10':>9}")
    print('  ' + '-' * (19 + 10 * len(horizons) + 9))
    for lo, hi in ((1.0, 1.1), (1.1, 1.2), (1.2, 1.4), (1.4, 99)):
        sub = sig[(sig['ratio'] >= lo) & (sig['ratio'] < hi)]
        if len(sub) < 10:
            continue
        cells = ''.join(_f(np.nanmean(sub[f'h{h}'].to_numpy(dtype=float)), 10)
                        for h in horizons)
        h10 = sub['h10'].to_numpy(dtype=float) if 'h10' in sub else np.array([])
        h10 = h10[np.isfinite(h10)]
        w10 = 100.0 * (h10 > 0).mean() if len(h10) else np.nan
        print(f'  {f"{lo:.1f}-{hi:.1f}":>12}{len(sub):>7}{cells}{_f(w10, 9, 0)}')
    nr = ctrl[ctrl['kind'] == 'near']
    cells = ''.join(_f(np.nanmean(nr[f'h{h}'].to_numpy(dtype=float)), 10)
                    for h in horizons)
    print(f'  {"CONTROL":>12}{len(nr):>7}{cells}')
    print('  A real strength signal makes these rows INCREASE downward, away from')
    print('  the CONTROL row. Flat or decreasing means the ratio carries nothing.')

    print('\nHow to read p: the percentile of the signal mean inside a bootstrap of')
    print('TIME-MATCHED control means at the same n. ~50 = the signal sits exactly')
    print('where chance puts it. Only <5 or >95 is worth a second look, and even then')
    print(f'the edge must exceed +{BREAKEVEN_PCT:.2f}% to make money.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
