"""Broker abstraction. place() returns a Fill (possibly provisional) or None
when the order could not be executed; the caller journals both outcomes.
"""

from abc import ABC, abstractmethod

from ...models import Fill, Order


class Broker(ABC):
    mode: str

    @abstractmethod
    def place(self, order: Order, ltp: float) -> Fill | None:
        ...

    def close(self) -> None:
        pass
