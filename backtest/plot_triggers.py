"""
Visualise where slope_angle fires.

Three stacked panels on a shared time axis:
  1. LTP, with trade entries (triangles) and exits (x)
  2. the angle series + threshold line + every raw threshold crossing
  3. cumulative P&L per unit

Raw triggers and actual entries are drawn separately on purpose: most triggers
are suppressed by the open-position check and the cooldown, and seeing the gap
between "fired" and "traded" is usually the most informative part of the chart.

Usage:
  python backtest/plot_triggers.py --csv ticks.csv --price-mode pct --threshold 7.05 \
      --stop-pct 1.5 --target-pct 3 --max-hold 900 --cooldown 100 -o triggers.html
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Running this file puts backtest/ on sys.path[0], where backtest.py would shadow
# the backtest package. Drop the script's own directory, keep the repo root.
_HERE = Path(__file__).resolve().parent
sys.path[:] = [q for q in sys.path if q and Path(q).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))

from backtest.backtest import (build_signals, load_csv, load_influx,   # noqa: E402
                               metrics, simulate)


UP, DOWN, GREY, ANG = '#26a69a', '#ef5350', '#8b949e', '#7aa2f7'


def build(df, args):
    price = df['ltp'].to_numpy(float)
    t = df['time']
    kw = dict(thresh_mode=args.thresh_mode, window=args.window, q=args.q, k=args.k,
              long_only=not args.allow_short, require_convex=not args.no_convex)
    sig = build_signals(df, args.n1, args.n2, args.price_mode, threshold=args.threshold, **kw)
    idx, ang, thr = sig['index'], sig['angle'], sig['thr']
    fired = idx[sig['long'] | sig['short']]

    res = simulate(df, args.n1, args.n2, args.price_mode, args.threshold,
                   args.slippage_bps, args.cost_bps, args.stop_pct, args.target_pct,
                   args.max_hold, args.cooldown, args.flip,
                   exit_on_down_bend=args.exit_on_down_bend, **kw)
    tr, m = res['trades'], metrics(res['trades'])

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.5, 0.28, 0.22], vertical_spacing=0.045,
                        subplot_titles=('LTP with trade entries / exits',
                                        f'angle (n1={args.n1}, n2={args.n2}, '
                                        f'{args.price_mode}) vs threshold',
                                        'cumulative P&L per unit'))

    # ── panel 1: price ────────────────────────────────────────────────────────
    fig.add_trace(go.Scattergl(x=t, y=price, mode='lines', name='LTP',
                               line=dict(color=GREY, width=1),
                               hovertemplate='%{x|%H:%M:%S}  %{y:.2f}<extra></extra>'),
                  row=1, col=1)
    if not tr.empty:
        for side, colour, sym in (('LONG', UP, 'triangle-up'), ('SHORT', DOWN, 'triangle-down')):
            s = tr[tr.side == side]
            if s.empty:
                continue
            fig.add_trace(go.Scattergl(
                x=s.entry_t, y=s.entry_px, mode='markers', name=f'{side} entry',
                marker=dict(color=colour, size=11, symbol=sym,
                            line=dict(color='white', width=1)),
                customdata=np.stack([s.angle, s.pnl, s.exit_reason], axis=-1),
                hovertemplate=('%{x|%H:%M:%S}  entry %{y:.2f}<br>'
                               'angle %{customdata[0]:.2f}deg<br>'
                               'P&L %{customdata[1]:+.2f} (%{customdata[2]})<extra></extra>')),
                row=1, col=1)
        fig.add_trace(go.Scattergl(x=tr.exit_t, y=tr.exit_px, mode='markers', name='exit',
                                   marker=dict(color='#e3b341', size=7, symbol='x'),
                                   hovertemplate='%{x|%H:%M:%S}  exit %{y:.2f}<extra></extra>'),
                      row=1, col=1)
        # entry->exit connectors, green when the trade made money
        for _, row in tr.iterrows():
            fig.add_trace(go.Scattergl(
                x=[row.entry_t, row.exit_t], y=[row.entry_px, row.exit_px], mode='lines',
                line=dict(color=UP if row.pnl > 0 else DOWN, width=1, dash='dot'),
                showlegend=False, hoverinfo='skip'), row=1, col=1)

    # ── panel 2: angle ────────────────────────────────────────────────────────
    fig.add_trace(go.Scattergl(x=t.iloc[idx], y=ang, mode='lines', name='angle',
                               line=dict(color=ANG, width=0.8),
                               hovertemplate='%{x|%H:%M:%S}  %{y:.2f}deg<extra></extra>'),
                  row=2, col=1)
    if len(fired):
        fig.add_trace(go.Scattergl(x=t.iloc[fired], y=ang[np.isin(idx, fired)],
                                   mode='markers', name=f'entry trigger ({len(fired)})',
                                   marker=dict(color='#f778ba', size=4),
                                   hovertemplate='%{x|%H:%M:%S}  %{y:.2f}deg<extra></extra>'),
                      row=2, col=1)
    # the threshold is a moving series in adaptive modes, so draw it as a line
    if args.thresh_mode == 'fixed':
        fig.add_hline(y=args.threshold, line=dict(color='#f0883e', width=1, dash='dash'),
                      annotation_text=f'threshold {args.threshold}deg',
                      annotation_position='top left', row=2, col=1)
    else:
        lbl = (f'threshold ({args.thresh_mode} q={args.q}, w={args.window})'
               if args.thresh_mode == 'percentile'
               else f'threshold ({args.thresh_mode} k={args.k}, w={args.window})')
        fig.add_trace(go.Scattergl(x=t.iloc[idx], y=thr, mode='lines', name=lbl,
                                   line=dict(color='#f0883e', width=1.4, dash='dash'),
                                   hovertemplate='%{x|%H:%M:%S}  thr %{y:.2f}deg<extra></extra>'),
                      row=2, col=1)

    # ── panel 3: equity ───────────────────────────────────────────────────────
    if not tr.empty:
        fig.add_trace(go.Scattergl(x=tr.exit_t, y=tr.pnl.cumsum(), mode='lines+markers',
                                   name='cum P&L', line=dict(color=ANG, width=1.5),
                                   marker=dict(size=4)), row=3, col=1)
        fig.add_hline(y=0, line=dict(color=GREY, width=1), row=3, col=1)

    day = f'{t.iloc[0]:%Y-%m-%d}'
    fig.update_layout(
        title=(f'slope_angle triggers - {args.label or day}   |   '
               f'{len(fired)} entry triggers -> {m["trades"]} trades, '
               f'win {m["win_rate"]:.0f}%, total {m["total_pnl"]:+.2f}, '
               f'PF {m["profit_factor"]:.2f}'),
        template='plotly_white', height=940, hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.015, x=0),
        margin=dict(l=60, r=30, t=110, b=40))
    fig.update_yaxes(title_text='LTP (Rs)', row=1, col=1)
    fig.update_yaxes(title_text='degrees', row=2, col=1)
    fig.update_yaxes(title_text='Rs / unit', row=3, col=1)
    fig.update_xaxes(title_text='time (IST)', row=3, col=1)
    return fig, m, len(fired)


def main():
    ap = argparse.ArgumentParser(description='plot slope_angle triggers')
    ap.add_argument('--csv'); ap.add_argument('--influx', action='store_true')
    ap.add_argument('--measurement'); ap.add_argument('--date')
    ap.add_argument('--n1', type=int, default=50); ap.add_argument('--n2', type=int, default=80)
    ap.add_argument('--price-mode', choices=['abs', 'pct'], default='pct')
    ap.add_argument('--threshold', type=float, default=7.05, help='used when --thresh-mode fixed')
    ap.add_argument('--thresh-mode', choices=['fixed','percentile','mad'], default='percentile')
    ap.add_argument('--window', type=int, default=2000)
    ap.add_argument('--q', type=float, default=0.95)
    ap.add_argument('--k', type=float, default=5.0)
    ap.add_argument('--allow-short', action='store_true')
    ap.add_argument('--no-convex', action='store_true')
    ap.add_argument('--exit-on-down-bend', action='store_true')
    ap.add_argument('--slippage-bps', type=float, default=5.0)
    ap.add_argument('--cost-bps', type=float, default=0.0)
    ap.add_argument('--stop-pct', type=float, default=1.5)
    ap.add_argument('--target-pct', type=float, default=3.0)
    ap.add_argument('--max-hold', type=int, default=900)
    ap.add_argument('--cooldown', type=int, default=100)
    ap.add_argument('--flip', action='store_true')
    ap.add_argument('--label', default='')
    ap.add_argument('-o', '--out', default='triggers.html')
    args = ap.parse_args()

    df = load_csv(args.csv) if args.csv else load_influx(args.measurement, args.date)
    fig, m, n_fired = build(df, args)
    fig.write_html(args.out, include_plotlyjs='cdn', full_html=True)
    print(f'{len(df):,} ticks | {n_fired} raw triggers | {m["trades"]} trades '
          f'| total {m["total_pnl"]:+.2f} | -> {args.out}')


if __name__ == '__main__':
    main()
