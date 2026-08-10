"""Pre-trade risk checks (docs §8). Entries face all checks; exits are only
blocked when there is nothing to exit — reducing risk is always allowed.
"""

from ..config import settings
from ..models import Signal, SignalAction
from ..utils.logger import logger
from .position_tracker import PositionTracker


def _underlying_root(symbol: str) -> str:
    """First word of the trading symbol: 'SENSEX 78000 PE 13 AUG 26' -> 'SENSEX'.

    Deliberately crude. Every strike and expiry of one index collapses to one root,
    which is exactly the grouping that matters -- they all move with the same spot.
    """
    return (symbol or '').strip().split(' ', 1)[0].upper()


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

        # Adjacent strikes of one index are NOT independent trades. On 2026-08-10 the
        # engine opened six SENSEX PE strikes (78000-78500) within minutes; SENSEX rose
        # and all six lost 8.1-8.5%. That is one directional call sized 6x. It tripped
        # the daily loss limit by 10:15 and suppressed 170 signals over the remaining
        # five hours -- the fourth session truncated to a morning. One strike instead of
        # six would have lost ~4,000 rather than 23,895, and the day would have run.
        cap = settings.MAX_POSITIONS_PER_UNDERLYING
        if cap > 0:
            side = 'CE' if ' CE ' in f' {sig.symbol} ' else (
                   'PE' if ' PE ' in f' {sig.symbol} ' else '')
            root = _underlying_root(sig.symbol)
            same = [s for s in self._tracker.open_symbols()
                    if _underlying_root(s) == root
                    and (not side or f' {side} ' in f' {s} ')]
            if len(same) >= cap:
                return False, (f'{root}{" " + side if side else ""}: '
                               f'{len(same)} correlated position(s) open, cap {cap}')

        qty = sig.qty or settings.ORDER_QTY_DEFAULT
        notional = qty * sig.price
        if notional > settings.MAX_ORDER_NOTIONAL:
            return False, f'notional {notional:.0f} exceeds cap {settings.MAX_ORDER_NOTIONAL:.0f}'

        return True, ''
