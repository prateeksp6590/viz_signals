"""Stop / target / trailing exits for LIVE positions.

Until now these rules lived only in backtest/backtest.py, so a live position could
only close on a reverse bend or the EOD flatten — a materially different strategy
from the one being backtested.

Sizing the stop is not a detail. On BSE SENSEX 77500 CE (30 Jul, expiry day) a
genuine trigger at 12:30:56 was stopped out by the VERY NEXT TICK at -2.5%, and
then the option rose 108%. A single tick moved 5.25 points on a Rs 207 option;
tick-to-tick |move| that day had p95 = 1.48%, so a fixed 1.5% stop sits inside one
tick of noise. The same 1.5% stop was correct on 29 Jul when sigma was a third of
that. Hence: express stops as a MULTIPLE OF REALISED VOLATILITY, measured per
instrument at entry.
"""

from dataclasses import dataclass

from ..config import settings
from ..strategies.angle_math import rolling_sigma_pct, sigma_stop_pct
from ..utils.logger import logger


@dataclass
class ExitPlan:
    stop_pct: float          # hard stop, % of entry
    trail_pct: float         # trailing distance once armed, 0 = off
    trail_after_pct: float   # profit at which the trail arms
    target_pct: float        # hard target, 0 = let it run
    max_hold_s: float        # 0 = no cap
    sigma_pct: float         # realised vol at entry, for the log


class ExitManager:
    """One plan per open position, fixed at entry; re-evaluated every cycle."""

    def __init__(self):
        self._plans: dict[str, ExitPlan] = {}

    def plan_for(self, instrument_key: str, view) -> ExitPlan:
        sigma = 0.0
        try:
            s = view.ticks['ltp'].dropna().to_numpy()
            if s.size > settings.EXIT_SIGMA_WINDOW // 4:
                arr = rolling_sigma_pct(s, settings.EXIT_SIGMA_WINDOW)
                if arr.size and arr[-1] == arr[-1]:      # not NaN
                    sigma = float(arr[-1])
        except Exception:
            pass

        if settings.EXIT_STOP_SIGMA and sigma > 0:
            stop = float(sigma_stop_pct(sigma, settings.EXIT_STOP_SIGMA,
                                        settings.EXIT_SIGMA_HORIZON))
        else:
            stop = settings.EXIT_STOP_PCT
        if settings.EXIT_TRAIL_SIGMA and sigma > 0:
            trail = float(sigma_stop_pct(sigma, settings.EXIT_TRAIL_SIGMA,
                                         settings.EXIT_SIGMA_HORIZON))
        else:
            trail = settings.EXIT_TRAIL_PCT

        plan = ExitPlan(stop_pct=stop, trail_pct=trail,
                        trail_after_pct=settings.EXIT_TRAIL_AFTER_PCT or trail,
                        target_pct=settings.EXIT_TARGET_PCT,
                        max_hold_s=settings.EXIT_MAX_HOLD_SECS, sigma_pct=sigma)
        self._plans[instrument_key] = plan
        logger.info(f'exit plan {view.symbol}: stop {stop:.2f}% trail {trail:.2f}% '
                    f'after +{plan.trail_after_pct:.2f}% '
                    f'(sigma {sigma:.3f}%/tick)')
        return plan

    def forget(self, instrument_key: str) -> None:
        self._plans.pop(instrument_key, None)

    def check(self, pos, ltp: float, now_ts) -> str | None:
        """Exit reason, or None. Mirrors simulate() in backtest/backtest.py."""
        if ltp is None or pos is None or not pos.avg_entry:
            return None
        plan = self._plans.get(pos.instrument_key)
        if plan is None:
            return None

        excursion = (ltp - pos.avg_entry) / pos.avg_entry * pos.direction
        peak = pos.max_favorable / pos.avg_entry if pos.avg_entry else 0.0

        stop_level = -plan.stop_pct / 100.0 if plan.stop_pct else float('-inf')
        trailing = False
        if plan.trail_pct and peak >= plan.trail_after_pct / 100.0:
            lvl = peak - plan.trail_pct / 100.0
            if lvl > stop_level:
                stop_level, trailing = lvl, True

        if excursion <= stop_level:
            return 'trail' if trailing else 'stop'
        if plan.target_pct and excursion >= plan.target_pct / 100.0:
            return 'target'
        if plan.max_hold_s and pos.entry_ts:
            if (now_ts - pos.entry_ts).total_seconds() >= plan.max_hold_s:
                return 'max_hold'
        return None
