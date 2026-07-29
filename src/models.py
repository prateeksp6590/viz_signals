"""Core data contracts: Signal, Order, Fill, Position.

See docs/shaaru-aureus-signal-engine.md §5 for field semantics.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
import itertools


class SignalAction(str, Enum):
    ENTER_LONG  = 'ENTER_LONG'
    ENTER_SHORT = 'ENTER_SHORT'
    EXIT        = 'EXIT'


class Side(str, Enum):
    BUY  = 'BUY'
    SELL = 'SELL'


class OrderStatus(str, Enum):
    FILLED   = 'FILLED'
    PENDING  = 'PENDING'
    REJECTED = 'REJECTED'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Enum):
            out[k] = v.value
        else:
            out[k] = v
    return out


_order_seq = itertools.count(1)


@dataclass
class Signal:
    instrument_key: str
    symbol: str
    action: SignalAction
    price: float
    strategy: str
    confidence: float = 1.0
    qty: int | None = None          # None → ORDER_QTY_DEFAULT
    reason: str = ''
    meta: dict = field(default_factory=dict)   # numeric strategy diagnostics -> InfluxDB
    ts: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return _serialize(asdict(self))


@dataclass
class Order:
    instrument_key: str
    symbol: str
    side: Side
    qty: int
    signal_action: SignalAction
    strategy: str
    mode: str                        # 'paper' | 'live'
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    ts: datetime = field(default_factory=_utcnow)
    id: str = field(default_factory=lambda: f'ord-{next(_order_seq):06d}')

    def to_dict(self) -> dict:
        return _serialize(asdict(self))


@dataclass
class Fill:
    order_id: str
    instrument_key: str
    symbol: str
    side: Side
    qty: int
    price: float
    ts: datetime = field(default_factory=_utcnow)
    provisional: bool = False        # live order not yet confirmed complete

    def to_dict(self) -> dict:
        return _serialize(asdict(self))


@dataclass
class Position:
    instrument_key: str
    symbol: str
    side: str                        # 'LONG' | 'SHORT'
    qty: int
    avg_entry: float
    strategy: str
    entry_ts: datetime = field(default_factory=_utcnow)

    # mark-to-market state, updated every poll cycle
    last_price: float = 0.0
    unrealized_pnl: float = 0.0
    max_favorable: float = 0.0       # best price excursion since entry (₹/unit)
    max_adverse: float = 0.0         # worst price excursion since entry (₹/unit)

    # set on close
    closed: bool = False
    exit_price: float | None = None
    exit_ts: datetime | None = None
    realized_pnl: float | None = None

    @property
    def direction(self) -> int:
        return 1 if self.side == 'LONG' else -1

    def mark(self, price: float) -> None:
        self.last_price = price
        excursion = (price - self.avg_entry) * self.direction
        self.unrealized_pnl = excursion * self.qty
        self.max_favorable = max(self.max_favorable, excursion)
        self.max_adverse = min(self.max_adverse, excursion)

    def close(self, price: float, ts: datetime | None = None) -> float:
        self.mark(price)
        self.closed = True
        self.exit_price = price
        self.exit_ts = ts or _utcnow()
        self.realized_pnl = (price - self.avg_entry) * self.direction * self.qty
        self.unrealized_pnl = 0.0
        return self.realized_pnl

    def to_dict(self) -> dict:
        return _serialize(asdict(self))
