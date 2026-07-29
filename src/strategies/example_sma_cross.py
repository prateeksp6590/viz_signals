"""Placeholder strategy: SMA crossover on 1-min bars.

Exists only to make the scaffold runnable end-to-end. Replace with the
proprietary algorithm (see docs §6).
"""

from ..models import Signal, SignalAction
from .base import Strategy

FAST = 5
SLOW = 20


class ExampleSmaCross(Strategy):
    name = 'example_sma_cross'

    def generate_signals(self, view) -> list[Signal]:
        bars = view.bars('1min')
        if len(bars) < SLOW + 1:
            return []

        close = bars['close']
        fast_now,  slow_now  = close.tail(FAST).mean(), close.tail(SLOW).mean()
        fast_prev, slow_prev = close.iloc[-FAST-1:-1].mean(), close.iloc[-SLOW-1:-1].mean()
        ltp = view.ltp
        if ltp is None:
            return []

        crossed_up   = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if view.position is None and crossed_up:
            return [Signal(
                instrument_key=view.instrument_key, symbol=view.symbol,
                action=SignalAction.ENTER_LONG, price=ltp, strategy=self.name,
                reason=f'SMA{FAST} crossed above SMA{SLOW}',
            )]
        if view.position is not None and crossed_down:
            return [Signal(
                instrument_key=view.instrument_key, symbol=view.symbol,
                action=SignalAction.EXIT, price=ltp, strategy=self.name,
                reason=f'SMA{FAST} crossed below SMA{SLOW}',
            )]
        return []
