"""
Backtest / forward-test harness for the slope_angle strategy.

Replays a tick series through the SAME maths the live engine uses
(src/strategies/angle_math.py), so a backtested edge and a live signal can
never diverge.

Sources
-------
  --csv FILE                 two columns: time, ltp  (as exported from InfluxDB)
  --influx --measurement M --date YYYY-MM-DD
                             pulls from the viz_hedge tick_data bucket

Examples
--------
  # single run
  python backtest/backtest.py --csv ticks.csv --threshold 60 --price-mode abs

  # find a threshold that actually fires
  python backtest/backtest.py --csv ticks.csv --sweep

  # grid over the lookback geometry too
  python backtest/backtest.py --csv ticks.csv --sweep --grid
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.strategies.angle_math import (adaptive_threshold, angle_series,   # noqa: E402
                                       is_upward_bend, rolling_sigma_pct, sigma_stop_pct)

IST = 'Asia/Kolkata'


# ── data loading ──────────────────────────────────────────────────────────────

def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    tcol = next((c for c in df.columns if c.lower() in ('time', '_time', 'timestamp')), None)
    pcol = next((c for c in df.columns if c.lower() in ('ltp', 'price', 'close')), None)
    if tcol is None or pcol is None:
        raise SystemExit(f'CSV needs a time column and an ltp column; got {list(df.columns)}')
    df[tcol] = pd.to_datetime(df[tcol], format='mixed', utc=True).dt.tz_convert(IST)
    df = df[[tcol, pcol]].rename(columns={tcol: 'time', pcol: 'ltp'})
    df['ltp'] = pd.to_numeric(df['ltp'], errors='coerce')
    return df.dropna().sort_values('time').reset_index(drop=True)


def load_influx(measurement: str, date: str) -> pd.DataFrame:
    from dotenv import load_dotenv
    load_dotenv()
    from src.config import settings
    from src.services.influx_reader import InfluxReader
    day = pd.Timestamp(date, tz=IST)
    reader = InfluxReader({})
    df = reader.fetch_range.__wrapped__(reader, measurement, day, day + pd.Timedelta(days=1)) \
        if hasattr(reader.fetch_range, '__wrapped__') else \
        reader._query(measurement,
                      day.tz_convert('UTC').isoformat().replace('+00:00', 'Z'),
                      (day + pd.Timedelta(days=1)).tz_convert('UTC').isoformat().replace('+00:00', 'Z'))
    reader.close()
    if df.empty:
        raise SystemExit(f'No data for measurement "{measurement}" on {date} '
                         f'in bucket "{settings.INFLUX_BUCKET}"')
    out = df.reset_index()[['_time', 'ltp']].rename(columns={'_time': 'time'})
    out['time'] = pd.to_datetime(out['time'], utc=True).dt.tz_convert(IST)
    return out.dropna().sort_values('time').reset_index(drop=True)


# ── signal construction ───────────────────────────────────────────────────────

def build_signals(df, n1, n2, price_mode, thresh_mode='fixed', threshold=60.0,
                  window=2000, q=0.99, k=5.0, long_only=True, require_convex=True):
    """Per-sample angle, threshold and entry mask. Shared by simulate() and the plots."""
    price = df['ltp'].to_numpy(dtype=float)
    r = angle_series(price, n1, n2, price_mode)
    ang = r['angle_deg']
    adaptive = adaptive_threshold(ang, thresh_mode, window, q, k)
    thr = np.full(ang.shape, float(threshold)) if adaptive is None else adaptive

    fire = np.isfinite(ang) & np.isfinite(thr) & (ang >= thr)
    up = is_upward_bend(r['slope_base'], r['slope_full'], r['slope_recent'], require_convex)
    long_sig = fire & up
    short_sig = np.zeros_like(fire) if long_only else (fire & (r['slope_recent'] < 0))
    return {'r': r, 'index': r['index'], 'angle': ang, 'thr': thr, 'fire': fire,
            'long': long_sig, 'short': short_sig}


def simulate(df, n1, n2, price_mode, threshold=60.0, slippage_bps=5.0, cost_bps=0.0,
             stop_pct=0.0, target_pct=0.0, max_hold=0, cooldown=0, flip=False,
             thresh_mode='fixed', window=2000, q=0.99, k=5.0,
             long_only=True, require_convex=True, exit_on_down_bend=False,
             trail_pct=0.0, trail_after_pct=0.0,
             stop_sigma=0.0, trail_sigma=0.0, sigma_window=200, sigma_horizon=50) -> dict:
    """Walk the tick series once; return trades plus the angle diagnostics.

    Exit model
    ----------
    stop_pct        initial hard stop, as % of entry (kept tight)
    trail_pct       once the trade is `trail_after_pct` in profit, the stop
                    ratchets to (peak excursion - trail_pct) and never loosens.
                    Lets winners run instead of capping them at a fixed target.
    target_pct      optional hard target; 0 = let the trail decide (default)
    max_hold        optional tick cap; 0 = hold until stopped or EOD

    Excursions are tracked at tick resolution on the peak favourable price, so
    the trail is as tight as the data allows. We only have ltp (no per-tick
    OHLC), so an intra-tick spike below the trail is invisible -- real fills
    would be slightly worse than modelled.
    """
    price = df['ltp'].to_numpy(dtype=float)
    tsec = df['time'].astype('int64').to_numpy() / 1e9
    sig = build_signals(df, n1, n2, price_mode, thresh_mode, threshold,
                        window, q, k, long_only, require_convex)
    idx, ang, thr = sig['index'], sig['angle'], sig['thr']
    long_sig, short_sig, fire = sig['long'], sig['short'], sig['fire']
    s_rec = sig['r']['slope_recent']

    # volatility-scaled stops: fixed at entry from the instrument's own recent sigma
    sig_pct = rolling_sigma_pct(price, sigma_window) if (stop_sigma or trail_sigma) else None

    trades, pos = [], None
    last_exit_i = -10 ** 9
    slip = slippage_bps / 10_000.0

    def close(pos, n, why):
        exit_px = price[n] * (1 - slip * pos['dir'])
        gross = (exit_px - pos['entry_px']) * pos['dir']
        cost = (pos['entry_px'] + exit_px) * cost_bps / 10_000.0
        return {**pos, 'peak_pct': pos['peak'] * 100,
                'exit_i': n, 'exit_t': df['time'].iloc[n], 'exit_px': exit_px,
                'exit_reason': why, 'pnl': gross - cost,
                'pnl_pct': (gross - cost) / pos['entry_px'] * 100,
                'hold_ticks': n - pos['entry_i'], 'hold_s': tsec[n] - tsec[pos['entry_i']]}

    for kk, n in enumerate(idx):
        p = price[n]
        want_long, want_short = bool(long_sig[kk]), bool(short_sig[kk])

        if pos is not None:
            held = n - pos['entry_i']
            excursion = (p - pos['entry_px']) / pos['entry_px'] * pos['dir']
            if excursion > pos['peak']:
                pos['peak'] = excursion
            why = None

            # effective stop: the hard stop, ratcheted up by the trail once the
            # trade has earned trail_after_pct. max() => it can only tighten.
            eff_stop = pos.get('stop_pct', stop_pct)
            eff_trail = pos.get('trail_pct', trail_pct)
            stop_level = -eff_stop / 100.0 if eff_stop else -np.inf
            trailing = False
            if eff_trail and pos['peak'] >= trail_after_pct / 100.0:
                trail_level = pos['peak'] - eff_trail / 100.0
                if trail_level > stop_level:
                    stop_level, trailing = trail_level, True

            if excursion <= stop_level:
                why = 'trail' if trailing else 'stop'
            elif target_pct and excursion >= target_pct / 100.0:
                why = 'target'
            elif max_hold and held >= max_hold:
                why = 'max_hold'
            elif not long_only and ((want_long and pos['dir'] < 0) or (want_short and pos['dir'] > 0)):
                why = 'reverse'
            elif long_only and exit_on_down_bend and fire[kk] and s_rec[kk] < 0:
                why = 'down_bend'
            if why:
                trades.append(close(pos, n, why))
                pos, last_exit_i = None, n
                if not (flip and why == 'reverse'):
                    continue

        if pos is None and (want_long or want_short) and (n - last_exit_i) >= cooldown:
            d = 1 if want_long else -1
            pos = {'entry_i': n, 'entry_t': df['time'].iloc[n],
                   'entry_px': p * (1 + slip * d), 'dir': d,
                   'side': 'LONG' if d > 0 else 'SHORT', 'angle': ang[kk],
                   'thr': thr[kk], 'peak': 0.0, 'window_s': tsec[n] - tsec[n - n2]}
            if sig_pct is not None and np.isfinite(sig_pct[n]):
                if stop_sigma:
                    pos['stop_pct'] = float(sigma_stop_pct(sig_pct[n], stop_sigma, sigma_horizon))
                if trail_sigma:
                    pos['trail_pct'] = float(sigma_stop_pct(sig_pct[n], trail_sigma, sigma_horizon))

    if pos is not None:
        trades.append(close(pos, len(price) - 1, 'eod'))

    n_valid = int(np.isfinite(thr).sum())
    return {'trades': pd.DataFrame(trades), 'angle': ang, 'index': idx, 'thr': thr,
            'triggers': int((long_sig | short_sig).sum()), 'evaluated': n_valid}


def metrics(t: pd.DataFrame) -> dict:
    if t.empty:
        return {'trades': 0, 'win_rate': 0.0, 'total_pnl': 0.0, 'expectancy': 0.0,
                'profit_factor': 0.0, 'max_dd': 0.0, 'avg_hold_s': 0.0}
    wins, losses = t[t.pnl > 0], t[t.pnl <= 0]
    cum = t.pnl.cumsum()
    gross_w, gross_l = wins.pnl.sum(), -losses.pnl.sum()
    return {
        'trades': len(t),
        'win_rate': 100.0 * len(wins) / len(t),
        'total_pnl': t.pnl.sum(),
        'expectancy': t.pnl.mean(),
        'avg_win': wins.pnl.mean() if len(wins) else 0.0,
        'avg_loss': losses.pnl.mean() if len(losses) else 0.0,
        'profit_factor': (gross_w / gross_l) if gross_l > 0 else float('inf'),
        'max_dd': float((cum.cummax() - cum).max()),
        'avg_hold_s': t.hold_s.mean(),
    }


# ── reporting ─────────────────────────────────────────────────────────────────

def report(df, res, args):
    t, angle = res['trades'], res['angle']
    m = metrics(t)
    thr_desc = (f"threshold={args.threshold}deg (fixed)" if args.thresh_mode == 'fixed'
                else f"threshold={args.thresh_mode} w={args.window} "
                     + (f"q={args.q}" if args.thresh_mode == 'percentile' else f"k={args.k}"))
    print(f"\n{'='*78}\nSLOPE-ANGLE BACKTEST   n1={args.n1} n2={args.n2} "
          f"mode={args.price_mode} {thr_desc}"
          f"{'  LONG-ONLY' if not args.allow_short else ''}"
          f"{' +convex' if not args.no_convex else ''}")
    print(f"{'='*78}")
    print(f"ticks              : {len(df):,}   "
          f"{df.time.iloc[0]:%Y-%m-%d %H:%M} -> {df.time.iloc[-1]:%H:%M} IST")
    print(f"angle distribution : median {np.median(angle):.2f}  p90 {np.percentile(angle,90):.2f}  "
          f"p99 {np.percentile(angle,99):.2f}  max {angle.max():.2f}")
    print(f"entry triggers     : {res['triggers']:,}  "
          f"({100*res['triggers']/max(res['evaluated'],1):.2f}% of evaluated points)")
    if args.thresh_mode != 'fixed':
        th = res['thr'][np.isfinite(res['thr'])]
        print(f"adaptive threshold : min {th.min():.2f}  median {np.median(th):.2f}  "
              f"max {th.max():.2f} deg   (warm-up skipped {len(angle)-len(th):,} pts)")
    if t.empty:
        print("\nNo trades. Lower --threshold (see --sweep) or check --price-mode.\n")
        return
    print(f"\ntrades             : {m['trades']}      win rate: {m['win_rate']:.1f}%")
    print(f"total P&L / unit   : Rs {m['total_pnl']:+.2f}   expectancy Rs {m['expectancy']:+.3f}/trade")
    print(f"avg win / avg loss : Rs {m['avg_win']:+.2f} / Rs {m['avg_loss']:+.2f}"
          f"    profit factor {m['profit_factor']:.2f}")
    print(f"max drawdown       : Rs {m['max_dd']:.2f}      avg hold {m['avg_hold_s']:.0f}s "
          f"({t.hold_ticks.mean():.0f} ticks)")
    print(f"exit reasons       : {t.exit_reason.value_counts().to_dict()}")
    stale = (t.window_s > 120).sum()
    if stale:
        print(f"\n  WARNING  {stale}/{len(t)} entries ({100*stale/len(t):.0f}%) used an "
              f"{args.n2}-tick window spanning >120s of wall clock,\n"
              f"           i.e. straddling a feeder data gap. Those angles are not "
              f"measuring what they look like.")
    print(f"\nfirst {min(12,len(t))} trades:")
    show = t[['entry_t', 'side', 'angle', 'entry_px', 'exit_px', 'exit_reason', 'pnl', 'hold_s']].head(12).copy()
    show['entry_t'] = show['entry_t'].dt.strftime('%H:%M:%S')
    print(show.to_string(index=False, float_format=lambda v: f'{v:.2f}'))
    print()


def sweep(df, args):
    kw = dict(slippage_bps=args.slippage_bps, cost_bps=args.cost_bps,
              stop_pct=args.stop_pct, target_pct=args.target_pct, max_hold=args.max_hold,
              cooldown=args.cooldown, flip=args.flip, thresh_mode=args.thresh_mode,
              window=args.window, k=args.k, long_only=not args.allow_short,
              require_convex=not args.no_convex, exit_on_down_bend=args.exit_on_down_bend)
    geoms = [(args.n1, args.n2)] if not args.grid else \
            [(20, 40), (30, 60), (50, 80), (50, 120), (80, 160)]
    label = 'q' if args.thresh_mode == 'percentile' else \
            ('k' if args.thresh_mode == 'mad' else 'deg')
    print(f"\n{'='*80}\nSWEEP   price-mode={args.price_mode}  threshold-mode={args.thresh_mode}"
          f"{'  LONG-ONLY' if not args.allow_short else ''}\n{'='*80}")

    for n1, n2 in geoms:
        a = angle_series(df['ltp'].to_numpy(float), n1, n2, args.price_mode)['angle_deg']
        a = a[np.isfinite(a)]
        if a.size == 0:
            continue
        if args.thresh_mode == 'percentile':
            cands = [0.95, 0.97, 0.98, 0.99, 0.995, 0.998, 0.999]
        elif args.thresh_mode == 'mad':
            cands = [2, 3, 4, 5, 6, 8, 10]
        else:
            cands = sorted({round(float(np.percentile(a, x)), 2)
                            for x in (50, 75, 90, 95, 98, 99, 99.5)})
        print(f"\n n1={n1} n2={n2}   angle p50={np.median(a):.2f} "
              f"p90={np.percentile(a,90):.2f} p99={np.percentile(a,99):.2f} max={a.max():.2f}")
        print(f"  {label:>7}{'thr~':>8}{'trig':>7}{'trades':>7}{'win%':>7}"
              f"{'totPnL':>10}{'exp':>9}{'PF':>7}{'maxDD':>8}")
        print('  ' + '-' * 70)
        for c in cands:
            kw2 = dict(kw)
            if args.thresh_mode == 'percentile':
                kw2['q'] = c
            elif args.thresh_mode == 'mad':
                kw2['k'] = c
            res = simulate(df, n1, n2, args.price_mode,
                           threshold=(c if args.thresh_mode == 'fixed' else args.threshold), **kw2)
            m = metrics(res['trades'])
            th = res['thr'][np.isfinite(res['thr'])]
            pf = '   inf' if m['profit_factor'] == float('inf') else f"{m['profit_factor']:7.2f}"
            print(f"  {c:>7}{(np.median(th) if th.size else float('nan')):8.2f}"
                  f"{res['triggers']:7d}{m['trades']:7d}{m['win_rate']:7.1f}"
                  f"{m['total_pnl']:10.2f}{m['expectancy']:9.3f}{pf}{m['max_dd']:8.2f}")
    print()


def sweep_sigma(df, args):
    """Grid the stop and trail as multiples of realized per-tick sigma."""
    kw = dict(slippage_bps=args.slippage_bps, cost_bps=args.cost_bps, max_hold=args.max_hold,
              cooldown=args.cooldown, thresh_mode=args.thresh_mode, window=args.window,
              q=args.q, k=args.k, long_only=not args.allow_short,
              require_convex=not args.no_convex, target_pct=args.target_pct,
              sigma_window=args.sigma_window, sigma_horizon=args.sigma_horizon)
    stops, trails = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0], [0.0, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    print(f"\n{'='*92}\nSIGMA EXIT GRID   stop=K*sigma*sqrt({args.sigma_horizon})"
          f"   sigma over {args.sigma_window} ticks   ({args.price_mode}/q={args.q})\n{'='*92}")
    print("  cell: totPnL / PF / win% / trades          trail K=0 means no trailing stop")
    cols = ''.join((('%.2f' % t) if t else 'none').rjust(15) for t in trails)
    print('\n  ' + 'stopK / trailK'.rjust(14) + cols)
    print('  ' + '-' * 90)
    best = None
    for sp in stops:
        cells = []
        for tp in trails:
            res = simulate(df, args.n1, args.n2, args.price_mode, args.threshold,
                           stop_sigma=sp, trail_sigma=tp, **kw)
            m = metrics(res['trades']); pf = m['profit_factor']
            cells.append(f"{m['total_pnl']:+7.1f}/{(99.9 if pf==float('inf') else pf):4.2f}"
                         f"/{m['win_rate']:3.0f}/{m['trades']:3d}")
            if best is None or m['total_pnl'] > best[0]:
                best = (m['total_pnl'], sp, tp, m)
        print(f"  {sp:>14.2f}" + ''.join(c.rjust(15) for c in cells))
    if best:
        _, sp, tp, m = best
        print(f"\n  best: stopK {sp}  trailK {tp or 'none'}  ->  {m['total_pnl']:+.2f}, "
              f"PF {m['profit_factor']:.2f}, win {m['win_rate']:.0f}%, {m['trades']} trades, "
              f"maxDD {m['max_dd']:.2f}, avg hold {m['avg_hold_s']:.0f}s")
    print()


def sweep_exits(df, args):
    """Grid the initial stop against the trailing distance."""
    kw = dict(slippage_bps=args.slippage_bps, cost_bps=args.cost_bps, max_hold=args.max_hold,
              cooldown=args.cooldown, flip=args.flip, thresh_mode=args.thresh_mode,
              window=args.window, q=args.q, k=args.k, long_only=not args.allow_short,
              require_convex=not args.no_convex, target_pct=args.target_pct,
              trail_after_pct=args.trail_after_pct)
    stops  = [0.3, 0.4, 0.5, 0.6, 0.8]
    trails = [0.0, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0]
    print(f"\n{'='*86}\nEXIT GRID   initial stop x trailing stop"
          f"   ({args.price_mode}/{args.thresh_mode} q={args.q}, n1={args.n1} n2={args.n2},"
          f" max_hold={args.max_hold or 'off'})\n{'='*86}")
    print("  each cell: totPnL / PF / win% / trades        trail=0 means no trailing stop")
    hdr = 'stop \\ trail'
    cols = ''.join(('%.1f%%' % t if t else 'none').rjust(15) for t in trails)
    print('\n  ' + hdr.rjust(11) + cols)
    print('  ' + '-' * 84)
    best = None
    for sp in stops:
        cells = []
        for tp in trails:
            res = simulate(df, args.n1, args.n2, args.price_mode, args.threshold,
                           stop_pct=sp, trail_pct=tp, **kw)
            m = metrics(res['trades'])
            pf = m['profit_factor']
            cells.append(f"{m['total_pnl']:+7.1f}/{(99.9 if pf==float('inf') else pf):4.2f}"
                         f"/{m['win_rate']:3.0f}/{m['trades']:3d}")
            if best is None or m['total_pnl'] > best[0]:
                best = (m['total_pnl'], sp, tp, m)
        print(f"  {sp:>10.1f}%" + "".join(f"{c:>15}" for c in cells))
    if best:
        _, sp, tp, m = best
        print(f"\n  best: stop {sp}%  trail {tp or 'none'}%  ->  {m['total_pnl']:+.2f}, "
              f"PF {m['profit_factor']:.2f}, win {m['win_rate']:.0f}%, {m['trades']} trades, "
              f"maxDD {m['max_dd']:.2f}, avg hold {m['avg_hold_s']:.0f}s")
    print()


def main():
    ap = argparse.ArgumentParser(description='slope_angle backtester')
    src = ap.add_argument_group('source')
    src.add_argument('--csv')
    src.add_argument('--influx', action='store_true')
    src.add_argument('--measurement')
    src.add_argument('--date')
    ap.add_argument('--n1', type=int, default=50)
    ap.add_argument('--n2', type=int, default=80)
    ap.add_argument('--price-mode', choices=['abs', 'pct'], default='abs')
    ap.add_argument('--threshold', type=float, default=60.0, help='used when --thresh-mode fixed')
    ap.add_argument('--thresh-mode', choices=['fixed', 'percentile', 'mad'], default='percentile',
                    help='percentile = fire on the top (1-q) of the instrument\'s own recent angles')
    ap.add_argument('--window', type=int, default=2000, help='ticks in the adaptive window')
    ap.add_argument('--q', type=float, default=0.99, help='percentile mode quantile')
    ap.add_argument('--k', type=float, default=5.0, help='mad mode robust-sigma multiple')
    ap.add_argument('--allow-short', action='store_true',
                    help='off by default: downside is meant to be traded via the PE')
    ap.add_argument('--no-convex', action='store_true',
                    help='drop the slope_full > slope_base acceleration filter')
    ap.add_argument('--exit-on-down-bend', action='store_true')
    ap.add_argument('--slippage-bps', type=float, default=5.0)
    ap.add_argument('--cost-bps', type=float, default=0.0)
    ap.add_argument('--stop-pct', type=float, default=0.0, help='initial hard stop; 0 = disabled')
    ap.add_argument('--trail-pct', type=float, default=0.0,
                    help='trailing stop distance from peak excursion; 0 = disabled')
    ap.add_argument('--trail-after-pct', type=float, default=0.0,
                    help='only start trailing once this much profit is earned')
    ap.add_argument('--stop-sigma', type=float, default=0.0,
                    help='stop = K * rolling sigma * sqrt(horizon); overrides --stop-pct')
    ap.add_argument('--trail-sigma', type=float, default=0.0,
                    help='trailing distance as a sigma multiple; overrides --trail-pct')
    ap.add_argument('--sigma-window', type=int, default=200)
    ap.add_argument('--sigma-horizon', type=int, default=50)
    ap.add_argument('--sweep-sigma', action='store_true',
                    help='grid stop-sigma x trail-sigma')
    ap.add_argument('--sweep-exits', action='store_true',
                    help='grid stop-pct x trail-pct at the current threshold')
    ap.add_argument('--target-pct', type=float, default=0.0, help='0 = disabled')
    ap.add_argument('--max-hold', type=int, default=0, help='ticks; 0 = disabled')
    ap.add_argument('--cooldown', type=int, default=0, help='ticks to wait after an exit')
    ap.add_argument('--flip', action='store_true', help='reverse straight into the opposite side')
    ap.add_argument('--sweep', action='store_true')
    ap.add_argument('--grid', action='store_true', help='with --sweep: also vary n1/n2')
    ap.add_argument('--save-trades', help='write the trade list to this CSV')
    args = ap.parse_args()

    if args.csv:
        df = load_csv(args.csv)
    elif args.influx and args.measurement and args.date:
        df = load_influx(args.measurement, args.date)
    else:
        raise SystemExit('need --csv FILE, or --influx --measurement M --date YYYY-MM-DD')
    if len(df) <= args.n2:
        raise SystemExit(f'only {len(df)} ticks; need more than n2={args.n2}')

    if args.sweep_sigma:
        sweep_sigma(df, args)
        return
    if args.sweep_exits:
        sweep_exits(df, args)
        return
    if args.sweep:
        sweep(df, args)
        return
    res = simulate(df, args.n1, args.n2, args.price_mode, args.threshold, args.slippage_bps,
                   args.cost_bps, args.stop_pct, args.target_pct, args.max_hold,
                   args.cooldown, args.flip, thresh_mode=args.thresh_mode, window=args.window,
                   q=args.q, k=args.k, long_only=not args.allow_short,
                   require_convex=not args.no_convex,
                   exit_on_down_bend=args.exit_on_down_bend,
                   trail_pct=args.trail_pct, trail_after_pct=args.trail_after_pct,
                   stop_sigma=args.stop_sigma, trail_sigma=args.trail_sigma,
                   sigma_window=args.sigma_window, sigma_horizon=args.sigma_horizon)
    report(df, res, args)
    if args.save_trades and not res['trades'].empty:
        res['trades'].to_csv(args.save_trades, index=False)
        print(f'trades -> {args.save_trades}\n')


if __name__ == '__main__':
    main()
