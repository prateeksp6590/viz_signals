import json
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from .config import settings
from .models import Order, OrderStatus, Side, SignalAction
from .services.influx_reader import InfluxReader
from .services.journal import Journal
from .services.market_view import MarketData
from .services.position_tracker import PositionTracker
from .services.risk_gate import RiskGate
from .services.signal_engine import SignalEngine
from .services.brokers.paper import PaperBroker
from .strategies.slope_angle import SlopeAngleStrategy
from .utils.logger import logger
from .utils.sizing import quantity_for, underlying_of

IST = timezone(timedelta(hours=5, minutes=30))

# ── The algorithm drop-in point ───────────────────────────────────────────────
STRATEGY = SlopeAngleStrategy()      # params come from ANGLE_* env vars


def _now_ist() -> datetime:
    return datetime.now(IST)


def _ist_time(hhmm: str):
    h, m = hhmm.split(':')
    return _now_ist().replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def _load_symbol_map() -> dict[str, str]:
    """instrument_key -> trading_symbol from the NSE/BSE/MCX instrument masters."""
    key_set = set(settings.ANALYZE_INSTRUMENTS)
    symbol_map: dict[str, str] = {}
    data_dir = settings.NSE_JSON_PATH.parent
    for exch in ('NSE', 'BSE', 'MCX'):
        path = data_dir / f'{exch}.json'
        if not path.exists():
            continue
        try:
            with open(path, 'r') as f:
                for inst in json.load(f):
                    key = inst.get('instrument_key', '')
                    if key in key_set:
                        symbol_map[key] = inst['trading_symbol']
        except Exception as e:
            logger.warning(f'Could not read {path}: {e}')
    if not symbol_map:
        logger.warning(f'No instrument master found under {data_dir} - '
                       f'measurement names will use raw instrument keys')
    missing = key_set - symbol_map.keys()
    if missing:
        logger.warning(f'No trading symbol for {len(missing)} key(s): {sorted(missing)[:5]}')
    return symbol_map


def _make_broker():
    if settings.ORDER_MODE == 'live':
        from .services.brokers.upstox_live import UpstoxLiveBroker
        return UpstoxLiveBroker()
    if settings.ORDER_MODE == 'paper':
        return PaperBroker()
    return None  # signals_only


def _execute(sig, view, broker, tracker, journal) -> None:
    if sig.action == SignalAction.EXIT:
        pos = tracker.get_open(sig.instrument_key)
        side = Side.SELL if pos.side == 'LONG' else Side.BUY
        qty = pos.qty
    else:
        side = Side.BUY if sig.action == SignalAction.ENTER_LONG else Side.SELL
        # lots x lot_size from the instrument master, per underlying
        qty, why = quantity_for(sig.instrument_key, sig.symbol)
        qty = sig.qty or qty
        logger.debug(f'size {sig.symbol}: {why}')

    order = Order(
        instrument_key=sig.instrument_key, symbol=sig.symbol, side=side, qty=qty,
        signal_action=sig.action, strategy=sig.strategy, mode=broker.mode,
    )
    ltp = view.ltp if view.ltp is not None else sig.price
    fill = broker.place(order, ltp)
    journal.order(order)
    if fill:
        journal.fill(fill)
        tracker.apply_fill(order, fill)
    else:
        logger.error(f'Order not executed: {order.side.value} {order.symbol} x{order.qty}')


def main():
    errors = settings.validate()
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    # ── Market hours gate ─────────────────────────────────────────────────────
    start_dt, stop_dt = _ist_time(settings.ENGINE_START), _ist_time(settings.ENGINE_STOP)
    now = _now_ist()
    if now >= stop_dt:
        logger.error(f'Past engine stop time ({settings.ENGINE_STOP} IST). Exiting.')
        sys.exit(0)
    if now < start_dt:
        wait = (start_dt - now).total_seconds()
        logger.info(f'Waiting {wait:.0f}s until {settings.ENGINE_START} IST')
        time.sleep(wait)

    # ── Wire components ───────────────────────────────────────────────────────
    symbol_map = _load_symbol_map()
    reader  = InfluxReader(symbol_map)
    market  = MarketData(reader, symbol_map)
    journal = Journal()
    tracker = PositionTracker(journal)
    gate    = RiskGate(tracker, journal)
    engine  = SignalEngine(STRATEGY, market, tracker, journal)
    broker  = _make_broker()

    logger.info(f'viz_signals starting — mode={settings.ORDER_MODE} '
                f'strategy={STRATEGY.name} '
                f'instruments={len(settings.ANALYZE_INSTRUMENTS)} '
                f'poll={settings.POLL_INTERVAL_SECS}s')

    stop_event = threading.Event()

    def _shutdown(reason: str) -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        logger.info(f'Shutting down — {reason}')
        if broker and broker.mode == 'paper':
            for key in list(market.views):
                pos = tracker.get_open(key)
                if pos:
                    from .models import Signal
                    view = market.views[key]
                    price = view.ltp if view.ltp is not None else pos.last_price
                    sig = Signal(instrument_key=key, symbol=pos.symbol,
                                 action=SignalAction.EXIT, price=price,
                                 strategy=pos.strategy, reason='EOD flatten')
                    _execute(sig, view, broker, tracker, journal)
        elif broker and broker.mode == 'live' and tracker.open_count:
            logger.warning(f'{tracker.open_count} LIVE position(s) still open — '
                           f'relying on broker intraday auto square-off')
        logger.info(f'Final: {tracker.summary()}')
        journal.close()
        reader.close()

    signal.signal(signal.SIGINT,  lambda *_: (_shutdown('SIGINT'), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (_shutdown('SIGTERM'), sys.exit(0)))

    # ── Poll loop ─────────────────────────────────────────────────────────────
    while not stop_event.is_set():
        if _now_ist() >= stop_dt:
            _shutdown(f'engine stop time reached ({settings.ENGINE_STOP} IST)')
            break
        cycle_start = time.monotonic()
        t_read = t_calc = 0.0
        try:
            market.refresh()
            t_read = time.monotonic() - cycle_start
            tracker.update_marks(market)
            _t = time.monotonic()
            cycle_signals = engine.run_cycle()
            t_calc = time.monotonic() - _t
            for sig in cycle_signals:
                if broker is None:
                    continue  # signals_only — already journaled
                if gate.approve(sig):
                    _execute(sig, market.views[sig.instrument_key], broker,
                             tracker, journal)
        except Exception as e:
            logger.error(f'Cycle error: {e}', exc_info=True)

        elapsed = time.monotonic() - cycle_start
        if elapsed > settings.POLL_INTERVAL_SECS:
            # the loop cannot keep up: effective poll becomes `elapsed`, silently.
            logger.warning(
                f'Cycle took {elapsed:.1f}s > POLL_INTERVAL_SECS='
                f'{settings.POLL_INTERVAL_SECS}s  (influx read {t_read:.1f}s, '
                f'strategy {t_calc:.1f}s). '
                + ('Read-bound: lower INFLUX_QUERY_CHUNK, raise INFLUX_QUERY_WORKERS, '
                   'or trim ANALYZE_INSTRUMENTS.' if t_read > t_calc else
                   'Compute-bound: lower ANGLE_WINDOW or trim ANALYZE_INSTRUMENTS.'))
        stop_event.wait(max(0.0, settings.POLL_INTERVAL_SECS - elapsed))


if __name__ == '__main__':
    main()
