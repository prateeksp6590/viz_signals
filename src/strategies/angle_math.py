"""Pure numpy geometry for the slope-angle strategy.

No package/broker/InfluxDB imports live here on purpose: the backtester imports
this module directly, so backtest and live trading run byte-identical maths.

See slope_angle.py for the full derivation and the units discussion.
"""

import math

import numpy as np


N1_DEFAULT = 50
N2_DEFAULT = 80


def angle_series(prices, n1: int = N1_DEFAULT, n2: int = N2_DEFAULT,
                 price_mode: str = 'abs') -> dict:
    """Vectorised angle computation over a price array.

    Returns a dict of numpy arrays aligned to `index` (positions into `prices`
    at which the angle is defined, i.e. n >= n2). Used by BOTH the live
    strategy and the backtester, so the two can never drift apart.
    """
    if n2 <= n1 or n1 <= 0:
        raise ValueError(f'require 0 < n1 < n2 (got n1={n1}, n2={n2})')

    p = np.asarray(prices, dtype=float)
    empty = np.array([], dtype=float)
    if p.size <= n2:
        return {'index': np.array([], dtype=int), 'angle_deg': empty,
                'slope_base': empty, 'slope_full': empty, 'slope_recent': empty,
                'p_old': empty, 'p_mid': empty, 'p_new': empty}

    idx = np.arange(n2, p.size)
    p_old, p_mid, p_new = p[idx - n2], p[idx - n1], p[idx]

    if price_mode == 'pct':
        with np.errstate(divide='ignore', invalid='ignore'):
            d_base = 100.0 * (p_mid - p_old) / p_old
            d_full = 100.0 * (p_new - p_old) / p_old
            d_rec  = 100.0 * (p_new - p_mid) / p_mid
    elif price_mode == 'abs':
        d_base, d_full, d_rec = p_mid - p_old, p_new - p_old, p_new - p_mid
    else:
        raise ValueError(f"price_mode must be 'abs' or 'pct' (got {price_mode!r})")

    slope_base   = d_base / (n2 - n1)
    slope_full   = d_full / n2
    slope_recent = d_rec / n1
    angle = np.abs(np.degrees(np.arctan(slope_base) - np.arctan(slope_full)))

    return {'index': idx, 'angle_deg': angle, 'slope_base': slope_base,
            'slope_full': slope_full, 'slope_recent': slope_recent,
            'p_old': p_old, 'p_mid': p_mid, 'p_new': p_new}


def angle_at(prices, n1: int = N1_DEFAULT, n2: int = N2_DEFAULT,
             price_mode: str = 'abs') -> dict | None:
    """Angle at the LAST element of `prices`. None if there is not enough history."""
    p = np.asarray(prices, dtype=float)
    if p.size < n2 + 1:
        return None
    r = angle_series(p[-(n2 + 1):], n1, n2, price_mode)
    if r['angle_deg'].size == 0 or not math.isfinite(float(r['angle_deg'][-1])):
        return None
    return {k: float(v[-1]) for k, v in r.items() if k != 'index'}


# ── Adaptive thresholds ───────────────────────────────────────────────────────
# A fixed angle cannot survive a volatility regime change: the same 7 deg that is
# the top 1% of bends on a quiet morning is unremarkable in an expiry-day melt.
# So express the threshold as a position in the instrument's OWN recent angle
# distribution. The signal rate then stays roughly constant across instruments
# and regimes, and there is nothing per-symbol to retune.
#
# LOOK-AHEAD: every window is shifted by one sample, so the threshold applied at
# n is computed strictly from angles at n-1 and earlier. Never remove the shift.

THRESH_MODES = ('fixed', 'percentile', 'mad')


def adaptive_threshold(angle, mode: str = 'percentile', window: int = 2000,
                       q: float = 0.99, k: float = 5.0,
                       min_periods: int | None = None, floor: float = 0.0):
    """Trailing per-sample threshold array aligned to `angle` (NaN while warming up).

    percentile : rolling quantile `q` of the last `window` angles.
                 q=0.99 fires on the top 1% of recent bends.
    mad        : rolling median + k * 1.4826 * MAD (robust z-score).
                 MAD uses the contemporaneous rolling median, which is the usual
                 cheap approximation rather than a true nested rolling median.
    fixed      : returns None -- caller uses its scalar threshold.
    """
    import pandas as pd

    if mode == 'fixed':
        return None
    if mode not in THRESH_MODES:
        raise ValueError(f'mode must be one of {THRESH_MODES} (got {mode!r})')

    s = pd.Series(np.asarray(angle, dtype=float))
    mp = min_periods if min_periods is not None else max(50, window // 2)

    if mode == 'percentile':
        th = s.rolling(window, min_periods=mp).quantile(q)
    else:
        med = s.rolling(window, min_periods=mp).median()
        mad = (s - med).abs().rolling(window, min_periods=mp).median()
        th = med + k * 1.4826 * mad

    th = th.shift(1)                       # strictly past information
    if floor:
        th = th.clip(lower=floor)
    return th.to_numpy()


def is_upward_bend(slope_base, slope_full, slope_recent, require_convex: bool = True):
    """Long-side filter.

    The angle is unsigned, so it fires on both up- and down-bends. For a CE-only
    strategy we want the price bending UP:

      slope_recent > 0   the latest leg (n-N1 -> n) is rising
      slope_full > slope_base  the path is convex -- the recent leg is steeper
                         than the trajectory the earlier leg set, i.e.
                         ACCELERATING up rather than merely still-rising

    These are independent: price can rise while decelerating (p_old=100,
    p_mid=110, p_new=111 gives slope_recent>0 but slope_full<slope_base).
    Downside is meant to be captured by running the same signal on the PE.
    """
    up = np.asarray(slope_recent) > 0
    if not require_convex:
        return up
    return up & (np.asarray(slope_full) > np.asarray(slope_base))


def rolling_sigma_pct(prices, window: int = 200):
    """Rolling std of tick-to-tick % returns, aligned to `prices` (shifted 1 sample).

    A fixed-% stop cannot work across instruments or regimes for the same reason a
    fixed angle cannot: on BSE SENSEX 77500 CE one tick moves 0.19% at the median
    and 0.67% at p95, so a 0.3% stop is ~1.5 median ticks -- inside the noise.
    Express stops as a multiple of this sigma instead.
    """
    import pandas as pd
    p = pd.Series(np.asarray(prices, dtype=float))
    ret = p.pct_change() * 100.0
    return ret.rolling(window, min_periods=max(20, window // 4)).std().shift(1).to_numpy()


def sigma_stop_pct(sigma_pct, k: float = 1.0, horizon_ticks: int = 50,
                   lo: float = 0.15, hi: float = 8.0):
    """Convert a per-tick sigma into a stop distance in %, scaled by sqrt(horizon).

    Random-walk scaling: expected drift over H ticks ~ sigma * sqrt(H). Clipped to
    [lo, hi] so a dead-quiet patch cannot produce an absurdly tight stop.
    """
    s = np.asarray(sigma_pct, dtype=float) * k * np.sqrt(max(1, horizon_ticks))
    return np.clip(s, lo, hi)
