"""
Slope-angle divergence strategy ("angle break").

Geometry
--------
At evaluation point n we read three LTPs:

    p_old = ltp[n - N2]        # oldest   (default N2 = 80)
    p_mid = ltp[n - N1]        # middle   (default N1 = 50)
    p_new = ltp[n]             # newest

and build two rays sharing an origin at n-N2:

    base : (n-N2) -> (n-N1)    slope_base = (p_mid - p_old) / (N2 - N1)
    full : (n-N2) -> (n)       slope_full = (p_new - p_old) / N2

The signal is the angle between them:

    angle = | atan(slope_base) - atan(slope_full) |      [degrees]

Both rays start at the same point, so this angle measures how far the recent
leg has bent away from the trajectory the earlier leg established. It is a
curvature / acceleration detector, not a trend detector: a perfectly straight
run from n-N2 to n gives angle 0 no matter how steep it is.

Units matter
------------
atan() is only meaningful once both axes have a fixed scale. `price_mode`
picks that scale:

    'abs'   slope in rupees per tick    - as originally specified; the
                                          threshold is instrument-specific
    'pct'   slope in % of p_old per tick - scale-free; one threshold
                                          transfers across instruments

Measured on BSE SENSEX 77000 CE (2026-07-27, 6,169 ticks), a 60 deg threshold
fires 8x/day in 'abs' mode and literally never in 'pct' mode (max angle 27.5).
Always calibrate with:  python backtest/backtest.py --sweep

Direction
---------
The angle is unsigned, so direction comes from the most recent leg
(n-N1 -> n): positive slope -> ENTER_LONG, negative -> ENTER_SHORT.
"""

import math

from ..models import Signal, SignalAction
from .angle_math import (N1_DEFAULT, N2_DEFAULT, adaptive_threshold,  # noqa: F401
                         adaptive_threshold_latest, angle_at, angle_series,
                         is_upward_bend)
from .base import Strategy


class SlopeAngleStrategy(Strategy):
    """Long-only CE strategy. See angle_math.py for the geometry and the
    adaptive-threshold rationale. Trade the downside by pointing this same
    strategy at the PE, not by shorting."""

    name = 'slope_angle'

    def __init__(self, n1=None, n2=None, threshold_deg=None, price_mode=None,
                 thresh_mode=None, window=None, q=None, mad_k=None,
                 long_only=None, require_convex=None, exit_on_reverse=None):
        from ..config import settings
        g = lambda v, d: d if v is None else v
        self.n1 = g(n1, settings.ANGLE_N1)
        self.n2 = g(n2, settings.ANGLE_N2)
        self.threshold = g(threshold_deg, settings.ANGLE_THRESHOLD_DEG)
        self.price_mode = price_mode or settings.ANGLE_PRICE_MODE
        self.thresh_mode = thresh_mode or settings.ANGLE_THRESH_MODE
        self.window = g(window, settings.ANGLE_WINDOW)
        self.q = g(q, settings.ANGLE_Q)
        self.mad_k = g(mad_k, settings.ANGLE_MAD_K)
        self.long_only = g(long_only, settings.ANGLE_LONG_ONLY)
        self.require_convex = g(require_convex, settings.ANGLE_REQUIRE_CONVEX)
        self.exit_on_reverse = g(exit_on_reverse, settings.ANGLE_EXIT_ON_REVERSE)
        if self.n2 <= self.n1:
            raise ValueError(f'ANGLE_N2 must exceed ANGLE_N1 (got {self.n2} <= {self.n1})')

    # ticks needed before this strategy can emit anything
    @property
    def warmup_ticks(self) -> int:
        if self.thresh_mode == 'fixed':
            return self.n2 + 1
        return self.n2 + 1 + max(50, self.window // 2)

    def generate_signals(self, view) -> list[Signal]:
        ticks = view.ticks
        if ticks.empty or 'ltp' not in ticks.columns:
            return []
        s = ticks['ltp'].dropna()
        if len(s) < self.warmup_ticks:
            return []

        # Only as much history as the threshold window actually consumes. The view
        # holds LOOKBACK_MINUTES of ticks (~4,700), but the angle at n plus a
        # `window`-long history of angles needs only window + n2 + 2 prices.
        need = self.window + self.n2 + 2 if self.thresh_mode != 'fixed' else self.n2 + 1
        prices = s.to_numpy()[-need:]
        r = angle_series(prices, self.n1, self.n2, self.price_mode)
        ang = r['angle_deg']
        if ang.size == 0:
            return []

        adaptive = adaptive_threshold_latest(ang, self.thresh_mode, self.window,
                                             self.q, self.mad_k)
        thr = float(self.threshold) if adaptive is None else float(adaptive)
        angle = float(ang[-1])
        if not math.isfinite(angle) or not math.isfinite(thr) or angle < thr:
            return []

        up = bool(is_upward_bend(r['slope_base'][-1:], r['slope_full'][-1:],
                                 r['slope_recent'][-1:], self.require_convex)[0])
        if self.long_only and not up:
            return []
        if not up and r['slope_recent'][-1] == 0:
            return []

        ltp = float(s.iloc[-1])
        meta = {'angle_deg': angle, 'threshold_deg': thr,
                'slope_base': float(r['slope_base'][-1]),
                'slope_full': float(r['slope_full'][-1]),
                'slope_recent': float(r['slope_recent'][-1]),
                'n1': self.n1, 'n2': self.n2}
        mode_desc = (f'{self.thresh_mode} q={self.q}' if self.thresh_mode == 'percentile'
                     else self.thresh_mode)
        reason = (f'angle {angle:.2f} >= {thr:.2f}deg [{self.price_mode}/{mode_desc} '
                  f'n1={self.n1} n2={self.n2}{" convex" if self.require_convex else ""}]')

        if view.position is not None:
            is_long = view.position.side == 'LONG'
            if self.exit_on_reverse and is_long != up:
                return [Signal(instrument_key=view.instrument_key, symbol=view.symbol,
                               action=SignalAction.EXIT, price=ltp, strategy=self.name,
                               reason=f'reverse {reason}', meta=meta)]
            return []

        return [Signal(
            instrument_key=view.instrument_key, symbol=view.symbol,
            action=SignalAction.ENTER_LONG if up else SignalAction.ENTER_SHORT,
            price=ltp, strategy=self.name, reason=reason, meta=meta,
        )]
