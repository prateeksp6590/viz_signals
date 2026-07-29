"""Open positions, fills → position lifecycle, mark-to-market P&L, and
price-excursion tracking (MFE/MAE). One position per instrument in v1.
"""

from ..models import Fill, Order, Position, SignalAction
from ..utils.logger import logger
from .market_view import MarketData


class PositionTracker:
    def __init__(self, journal):
        self._journal = journal
        self._open: dict[str, Position] = {}
        self.closed: list[Position] = []
        self.daily_realized: float = 0.0

    # ── queries ───────────────────────────────────────────────────────────────

    def get_open(self, instrument_key: str) -> Position | None:
        return self._open.get(instrument_key)

    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def total_unrealized(self) -> float:
        return sum(p.unrealized_pnl for p in self._open.values())

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def apply_fill(self, order: Order, fill: Fill) -> None:
        if order.signal_action == SignalAction.EXIT:
            pos = self._open.pop(order.instrument_key, None)
            if not pos:
                logger.warning(f'Exit fill with no open position: {order.symbol}')
                return
            realized = pos.close(fill.price, fill.ts)
            self.daily_realized += realized
            self.closed.append(pos)
            self._journal.position(pos, event='close')
            logger.info(
                f'CLOSED {pos.side} {pos.symbol} x{pos.qty} '
                f'{pos.avg_entry:.2f} → {fill.price:.2f}  P&L {realized:+.2f}  '
                f'(day realized {self.daily_realized:+.2f})'
            )
        else:
            side = 'LONG' if order.signal_action == SignalAction.ENTER_LONG else 'SHORT'
            pos = Position(
                instrument_key=order.instrument_key,
                symbol=order.symbol,
                side=side,
                qty=fill.qty,
                avg_entry=fill.price,
                strategy=order.strategy,
                entry_ts=fill.ts,
            )
            pos.mark(fill.price)
            self._open[order.instrument_key] = pos
            self._journal.position(pos, event='open')
            logger.info(f'OPENED {side} {pos.symbol} x{pos.qty} @ {fill.price:.2f}')

    def update_marks(self, market: MarketData) -> None:
        for key, pos in self._open.items():
            view = market.views.get(key)
            ltp = view.ltp if view else None
            if ltp is not None:
                pos.mark(ltp)
            self._journal.position(pos, event='mark')

    def summary(self) -> str:
        return (
            f'positions open={self.open_count} closed={len(self.closed)} '
            f'realized={self.daily_realized:+.2f} unrealized={self.total_unrealized:+.2f}'
        )
