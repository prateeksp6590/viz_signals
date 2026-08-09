"""Slope-angle on the OHLC MEAN of completed bars, instead of on raw ticks.

WHY THIS EXISTS
---------------
The tick-level slope_angle strategy fires on every print, so it inherits bid-ask
bounce directly: measured autocorr(1) = -0.22 on SENSEX option ticks, i.e. a large
share of consecutive moves are the price alternating between bid and ask with no
information. The mean of open/high/low/close is a 4-point average of a whole
minute, which suppresses single-print extremes without the lag of an EMA.

MEASURED BEFORE DEPLOYING — READ THIS
-------------------------------------
A close relative of this (slope-angle on the mean of OPEN and CLOSE, 1-min bars,
n1/n2 = 5/8, q = 0.50) was scored in strategy_research.ipynb across 30 series:

    256 trades   46% win   median -412   positive on 2 of 4 days      (4-day pilot)
    ...          45% win   NET -439,035  positive on 5 of 30 series   (wider pull)

It is a KNOWN-NEGATIVE detector on the history available. This module exists to
gather live data under a different configuration, NOT because the edge is expected.
Run it in ORDER_MODE=paper. If it is ever switched to live, that is a decision to
trade something the research says loses.

THE TWO TRAPS THIS CODE AVOIDS
------------------------------
1. THE FORMING BAR. view.bars() resamples every tick held in memory, so the LAST
   bar is partial and its OHLC mean changes on every poll. Signalling off it means
   acting on a bar that does not exist yet, and it is not what the backtest scored.
   We drop it and only ever use completed bars.

2. RE-FIRING WITHIN A BAR. The engine polls ~1/second; a 1-minute bar would emit
   the same signal ~60 times. We remember the last completed bar per instrument and
   emit at most one signal per bar.
"""

import pandas as pd

from ..models import Signal, SignalAction
from .angle_math import (adaptive_threshold_latest, angle_at, angle_series,
                         is_upward_bend)
from .base import Strategy


class OhlcMeanSlopeStrategy(Strategy):
    """Slope-angle over the (O+H+L+C)/4 series of completed bars.

    n1/n2 are in BARS, not ticks. The tick strategy's 50/80 was chosen for ~1s
    prints; on 1-minute bars that would demand 80 minutes of history per signal and
    fire a handful of times a day, so the defaults are much smaller.
    """

    name = 'ohlc_mean_slope'

    def __init__(self, interval=None, n1=None, n2=None, q=None, window=None,
                 min_samples=None, price_mode=None, long_only=None,
                 require_convex=None):
        from ..config import settings
        g = lambda v, d: d if v is None else v
        self.interval = interval or settings.OHLC_SLOPE_INTERVAL
        self.n1 = g(n1, settings.OHLC_SLOPE_N1)
        self.n2 = g(n2, settings.OHLC_SLOPE_N2)
        self.q = g(q, settings.OHLC_SLOPE_Q)
        self.window = g(window, settings.OHLC_SLOPE_WINDOW)
        self.min_samples = g(min_samples, settings.OHLC_SLOPE_MIN_SAMPLES)
        # 'pct' so one threshold means the same thing on a Rs 40 and a Rs 600 option
        self.price_mode = price_mode or 'pct'
        self.long_only = g(long_only, settings.ANGLE_LONG_ONLY)
        self.require_convex = g(require_convex, settings.ANGLE_REQUIRE_CONVEX)
        if self.n2 <= self.n1:
            raise ValueError(f'OHLC_SLOPE_N2 must exceed N1 '
                             f'(got {self.n2} <= {self.n1})')
        self._last_bar: dict[str, pd.Timestamp] = {}

    def generate_signals(self, view) -> list[Signal]:
        bars = view.bars(self.interval)
        # need n2+1 points for the angle, +1 more for the bar we discard
        if bars is None or len(bars) < self.n2 + 2:
            return []

        completed = bars.iloc[:-1]          # trap 1: never signal off the forming bar
        stamp = completed.index[-1]
        key = view.instrument_key
        if self._last_bar.get(key) == stamp:
            return []                       # trap 2: one signal per bar, not per poll
        self._last_bar[key] = stamp

        cols = [c for c in ('open', 'high', 'low', 'close') if c in completed]
        if len(cols) < 4:
            return []
        mean = completed[cols].mean(axis=1).dropna()
        if len(mean) < self.n2 + 1:
            return []

        a = angle_at(mean.values, self.n1, self.n2, price_mode=self.price_mode)
        if not a:
            return []

        # angle_series/angle_at name the angle 'angle_deg', not 'angle'.
        hist = angle_series(mean.values, self.n1, self.n2,
                            price_mode=self.price_mode)['angle_deg']
        thr = adaptive_threshold_latest(hist, mode='percentile', window=self.window,
                                        q=self.q, min_periods=self.min_samples)
        if thr is None or not (a['angle_deg'] >= thr):
            return []

        up = is_upward_bend(a['slope_base'], a['slope_full'], a['slope_recent'],
                            require_convex=self.require_convex)
        if up:
            action = SignalAction.ENTER_LONG
        elif self.long_only:
            return []
        else:
            # mirror condition: bending DOWN, and accelerating downward
            down = (a['slope_recent'] < 0 and
                    (not self.require_convex or a['slope_full'] < a['slope_base']))
            if not down:
                return []
            action = SignalAction.ENTER_SHORT

        price = float(view.ltp if view.ltp is not None else mean.iloc[-1])
        return [Signal(
            instrument_key=key, symbol=view.symbol, action=action, price=price,
            strategy=self.name,
            reason=(f'ohlc_mean_slope: angle {a["angle_deg"]:.2f} >= {thr:.2f}deg '
                    f'[{self.interval} O H L C mean, n1={self.n1} n2={self.n2} '
                    f'q={self.q}]'))]
