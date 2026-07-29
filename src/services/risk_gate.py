"""Pre-trade risk checks (docs §8). Entries face all checks; exits are only
blocked when there is nothing to exit — reducing risk is always allowed.
"""

from ..config import settings
from ..models import Signal, SignalAction
from ..utils.logger import logger
from .position_tracker import PositionTracker


class RiskGate:
    def __init__(self, tracker: PositionTracker, journal):
        self._tracker = tracker
        self._journal = journal
        self._loss_halted = False

    def approve(self, sig: Signal) -> bool:
        ok, reason = self._evaluate(sig)
        if not ok:
            logger.warning(f'RISK REJECT [{reason}]: {sig.action.value} {sig.symbol}')
            self._journal.rejection(sig, reason)
        return ok

    def _evaluate(self, sig: Signal) -> tuple[bool, str]:
        if sig.action == SignalAction.EXIT:
            return True, ''

        if sig.instrument_key.startswith('NSE_INDEX'):
            return False, 'index instruments are signal-only'

        if settings.KILL_SWITCH_FILE.exists():
            return False, f'kill switch active ({settings.KILL_SWITCH_FILE})'

        if self._loss_halted:
            return False, 'daily loss limit breached — entries halted'
        total_pnl = self._tracker.daily_realized + self._tracker.total_unrealized
        if total_pnl <= -settings.DAILY_LOSS_LIMIT:
            self._loss_halted = True
            logger.error(
                f'DAILY LOSS LIMIT breached (P&L {total_pnl:.2f} ≤ '
                f'-{settings.DAILY_LOSS_LIMIT}) — no further entries today'
            )
            return False, 'daily loss limit breached — entries halted'

        if self._tracker.open_count >= settings.MAX_OPEN_POSITIONS:
            return False, f'max open positions ({settings.MAX_OPEN_POSITIONS}) reached'

        qty = sig.qty or settings.ORDER_QTY_DEFAULT
        notional = qty * sig.price
        if notional > settings.MAX_ORDER_NOTIONAL:
            return False, f'notional {notional:.0f} exceeds cap {settings.MAX_ORDER_NOTIONAL:.0f}'

        return True, ''
