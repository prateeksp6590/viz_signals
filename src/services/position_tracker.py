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
        self.daily_realized: float = self._restore_daily_realized()

    # ── queries ───────────────────────────────────────────────────────────────

    @staticmethod
    def _restore_daily_realized() -> float:
        """Re-read today's realised P&L from the journal at startup.

        daily_realized used to live only in memory, so every `systemctl restart`
        reset it to zero — and with it the DAILY_LOSS_LIMIT. On 2026-08-03 the
        service was restarted six times and realised P&L reached -39,269 against a
        -10,000 limit that was, technically, never breached by any single process.
        """
        import json
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from ..config import settings
        ist = timezone(timedelta(hours=5, minutes=30))
        f = (Path(settings.JOURNAL_DIR) / datetime.now(ist).strftime('%Y%m%d')
             / 'positions.jsonl')
        if not f.exists():
            return 0.0
        total = 0.0
        try:
            for line in f.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get('event') == 'close' and r.get('realized_pnl') is not None:
                    total += float(r['realized_pnl'])
        except Exception as e:
            logger.warning(f'could not restore today\'s realised P&L: {e}')
            return 0.0
        if total:
            logger.warning(f'Restored realised P&L from journal: {total:+.2f} — '
                           f'the daily loss limit continues from here, not from zero')
        return total

    def get_open(self, instrument_key: str) -> Position | None:
        return self._open.get(instrument_key)

    def open_symbols(self) -> list[str]:
        """Symbols of every currently open position, for correlation checks."""
        return [p.symbol for p in self._open.values()]

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
