"""Runs the strategy against each instrument view and filters raw signals
for eligibility before they reach the risk gate.

Eligibility rules (docs §8):
  1. ENTER_* suppressed when a position is already open on the instrument
  2. EXIT suppressed when no position is open
  3. Per-(instrument, action) cooldown of SIGNAL_COOLDOWN_SECS
"""

from datetime import datetime, timezone

from ..config import settings
from ..models import Signal, SignalAction
from ..utils.logger import logger
from .market_view import MarketData
from .position_tracker import PositionTracker


class SignalEngine:
    def __init__(self, strategy, market: MarketData, tracker: PositionTracker, journal):
        self._strategy = strategy
        self._market = market
        self._tracker = tracker
        self._journal = journal
        self._last_emitted: dict[tuple[str, str], datetime] = {}
        self._thin_warned: set[str] = set()

    def run_cycle(self) -> list[Signal]:
        """Returns eligible signals for this cycle (all raw signals are journaled)."""
        eligible: list[Signal] = []
        for key, view in self._market.views.items():
            view.position = self._tracker.get_open(key)
            if view.ticks.empty:
                continue
            need = getattr(self._strategy, 'warmup_ticks', 0)
            if need and len(view.ticks) < need:
                if key not in self._thin_warned and view.last_ts is not None:
                    age_min = (view.ticks.index[-1] - view.ticks.index[0]).total_seconds() / 60
                    if age_min > 90:      # had plenty of wall clock and still too few
                        self._thin_warned.add(key)
                        logger.warning(
                            f'{view.symbol}: only {len(view.ticks)} ticks in '
                            f'{age_min:.0f} min — needs {need}. Too illiquid for this '
                            f'strategy; it will not signal.')
                continue
            try:
                raw = self._strategy.generate_signals(view) or []
            except Exception as e:
                logger.error(f'Strategy error on {view.symbol}: {e}')
                continue

            for sig in raw:
                self._journal.signal(sig)
                ok, why = self._check(sig)
                if ok:
                    eligible.append(sig)
                    logger.info(
                        f'SIGNAL {sig.action.value} {sig.symbol} @ {sig.price} '
                        f'({sig.strategy}: {sig.reason})'
                    )
                else:
                    logger.debug(f'Signal suppressed [{why}]: {sig.action.value} {sig.symbol}')
        return eligible

    def _check(self, sig: Signal) -> tuple[bool, str]:
        open_pos = self._tracker.get_open(sig.instrument_key)
        if sig.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT) and open_pos:
            return False, 'position already open'
        if sig.action == SignalAction.EXIT and not open_pos:
            return False, 'no open position'

        cd_key = (sig.instrument_key, sig.action.value)
        last = self._last_emitted.get(cd_key)
        now = datetime.now(timezone.utc)
        if last and (now - last).total_seconds() < settings.SIGNAL_COOLDOWN_SECS:
            return False, 'cooldown'
        self._last_emitted[cd_key] = now
        return True, ''
