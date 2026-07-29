"""Live broker — Upstox v3 Place Order API (intraday market orders).

v1 limitation (docs §9): fills are confirmed by polling order details for a
few seconds after placement. If confirmation doesn't arrive in time the fill
is returned as provisional at LTP and the order stays PENDING in the journal.
The Upstox portfolio-stream WebSocket replaces this in a later phase.
"""

import time

import requests

from ...config import settings
from ...models import Fill, Order, OrderStatus
from ...utils.logger import logger
from .base import Broker

LIVE_BASE    = 'https://api.upstox.com'
SANDBOX_BASE = 'https://api-sandbox.upstox.com'

_FILL_POLL_ATTEMPTS = 5
_FILL_POLL_DELAY_S  = 1.0


class UpstoxLiveBroker(Broker):
    mode = 'live'

    def __init__(self):
        self._base = SANDBOX_BASE if settings.UPSTOX_ORDER_SANDBOX else LIVE_BASE
        self._headers = {
            'Authorization': f'Bearer {settings.UPSTOX_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if settings.UPSTOX_ORDER_SANDBOX:
            logger.warning('UpstoxLiveBroker in SANDBOX mode — orders go to api-sandbox')

    def place(self, order: Order, ltp: float) -> Fill | None:
        body = {
            'quantity': order.qty,
            'product': 'I',                     # intraday
            'validity': 'DAY',
            'price': 0,
            'tag': 'viz_signals',
            'instrument_token': order.instrument_key,
            'order_type': 'MARKET',
            'transaction_type': order.side.value,
            'disclosed_quantity': 0,
            'trigger_price': 0,
            'is_amo': False,
            'slice': False,
        }
        try:
            res = requests.post(
                f'{self._base}/v3/order/place', headers=self._headers, json=body, timeout=10
            )
        except requests.RequestException as e:
            logger.error(f'Order placement failed for {order.symbol}: {e}')
            order.status = OrderStatus.REJECTED
            return None

        if not res.ok:
            logger.error(f'Order rejected by Upstox ({res.status_code}): {res.text}')
            order.status = OrderStatus.REJECTED
            return None

        order_ids = res.json().get('data', {}).get('order_ids', [])
        if not order_ids:
            logger.error(f'No order id in Upstox response: {res.text}')
            order.status = OrderStatus.REJECTED
            return None
        order.broker_order_id = order_ids[0]

        outcome, avg_price = self._poll_fill(order.broker_order_id)
        if outcome == 'rejected':
            order.status = OrderStatus.REJECTED
            return None
        if outcome == 'filled':
            order.status = OrderStatus.FILLED
            return Fill(
                order_id=order.id,
                instrument_key=order.instrument_key,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                price=avg_price,
            )

        logger.warning(
            f'Fill not confirmed for {order.symbol} (broker id {order.broker_order_id}) '
            f'— marking provisionally at LTP {ltp}'
        )
        order.status = OrderStatus.PENDING
        return Fill(
            order_id=order.id,
            instrument_key=order.instrument_key,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=ltp,
            provisional=True,
        )

    def _poll_fill(self, broker_order_id: str) -> tuple[str, float | None]:
        """Returns ('filled', avg_price) | ('rejected', None) | ('unknown', None).

        Order details live on the production API even for sandbox placements;
        in sandbox this stays 'unknown' — the provisional-fill path applies.
        """
        for _ in range(_FILL_POLL_ATTEMPTS):
            time.sleep(_FILL_POLL_DELAY_S)
            try:
                res = requests.get(
                    f'{LIVE_BASE}/v2/order/details',
                    params={'order_id': broker_order_id},
                    headers=self._headers,
                    timeout=10,
                )
                if not res.ok:
                    continue
                data = res.json().get('data', {})
                if data.get('status') == 'complete' and data.get('average_price'):
                    return 'filled', float(data['average_price'])
                if data.get('status') in ('rejected', 'cancelled'):
                    logger.error(f'Broker order {broker_order_id} {data.get("status")}: '
                                 f'{data.get("status_message", "")}')
                    return 'rejected', None
            except requests.RequestException as e:
                logger.warning(f'Order details poll failed: {e}')
        return 'unknown', None
