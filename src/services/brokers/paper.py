"""Paper broker — immediate simulated fill at LTP ± SLIPPAGE_BPS.
Buys pay up, sells give up; a crude but conservative cost model.
"""

from ...config import settings
from ...models import Fill, Order, OrderStatus, Side
from .base import Broker


class PaperBroker(Broker):
    mode = 'paper'

    def place(self, order: Order, ltp: float) -> Fill | None:
        slip = settings.SLIPPAGE_BPS / 10_000
        price = ltp * (1 + slip) if order.side == Side.BUY else ltp * (1 - slip)
        order.status = OrderStatus.FILLED
        return Fill(
            order_id=order.id,
            instrument_key=order.instrument_key,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=round(price, 2),
        )
