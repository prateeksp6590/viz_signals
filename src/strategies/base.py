"""Strategy interface — the drop-in point for the proprietary algorithm.

Implement generate_signals(view) and wire the class in src/main.py.
See docs/shaaru-aureus-signal-engine.md §6 for the InstrumentView contract.
"""

from abc import ABC, abstractmethod

from ..models import Signal
from ..services.market_view import InstrumentView


class Strategy(ABC):
    name: str = 'unnamed'

    @abstractmethod
    def generate_signals(self, view: InstrumentView) -> list[Signal]:
        """Called once per instrument per poll cycle. Return [] for no action."""
        ...
