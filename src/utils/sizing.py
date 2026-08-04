"""Resolve an instrument to a tradeable quantity: lots x lot_size.

lot_size comes from the instrument master, never a constant. Exchanges revise lot
sizes (NIFTY has moved repeatedly) and a stale hardcoded value silently misprices
every trade in the report without any error.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from ..config import settings


@lru_cache(maxsize=1)
def _master_index() -> dict:
    """instrument_key -> {'symbol', 'underlying', 'lot_size'} across all exchanges."""
    out, data_dir = {}, settings.NSE_JSON_PATH.parent
    for ex in ('NSE', 'BSE', 'MCX'):
        p = data_dir / f'{ex}.json'
        if not p.exists():
            continue
        try:
            rows = json.loads(p.read_text())
        except Exception:
            continue
        for r in rows:
            k = r.get('instrument_key')
            if not k:
                continue
            out[k] = {
                'symbol': r.get('trading_symbol', ''),
                'underlying': (r.get('underlying_symbol') or r.get('asset_symbol')
                               or r.get('name') or '').strip().upper(),
                'lot_size': int(r.get('lot_size') or 0),
            }
    return out


def underlying_candidates(instrument_key: str, symbol: str = '') -> list[str]:
    """Every plausible name for the underlying, best guess first.

    MCX options are written on a FUTURES contract, so `underlying_symbol` is that
    contract (e.g. 'CRUDEOILM 17AUG26'), not the commodity — which is why
    LOTS_BY_UNDERLYING['CRUDEOILM'] never matched and sizing fell back to
    ORDER_QTY_DEFAULT=1. At qty 1 no move can cover the ~Rs 48 round-trip cost, so
    every MCX trade on 2026-08-03 was a guaranteed loss.
    """
    out, info = [], _master_index().get(instrument_key)
    if info:
        out.append(info['underlying'])
    for src in (info['symbol'] if info else '', symbol):
        m = re.match(r'^([A-Z&]+)', (src or '').upper())
        if m:
            out.append(m.group(1))          # leading word: 'CRUDEOILM 7650 CE ...'
    seen, uniq = set(), []
    for x in out:
        x = (x or '').strip().upper()
        if x and x not in seen:
            seen.add(x); uniq.append(x)
    return uniq


def underlying_of(instrument_key: str, symbol: str = '') -> str:
    """The candidate that LOTS_BY_UNDERLYING actually knows, else the first."""
    cands = underlying_candidates(instrument_key, symbol)
    for c in cands:
        if c in settings.LOTS_BY_UNDERLYING:
            return c
    return cands[0] if cands else ''


def lot_size_of(instrument_key: str) -> int:
    info = _master_index().get(instrument_key)
    return info['lot_size'] if info else 0


def is_equity(instrument_key: str) -> bool:
    return instrument_key.split('|', 1)[0].upper().endswith('_EQ')


def quantity_for(instrument_key: str, symbol: str = '',
                 price: float | None = None) -> tuple[int, str]:
    """(quantity, explanation). Returns 0 when the instrument cannot be sized.

    0 means DO NOT TRADE. Guessing a quantity is worse than skipping: an unsized
    trade still pays full brokerage and STT, so it is negative-expectancy by
    construction regardless of whether the signal was right.
    """
    if is_equity(instrument_key):
        # MIS margin is ~1/5th of notional, so size by capital deployed rather than
        # a share count — otherwise a Rs 200 stock and a Rs 14,000 stock would carry
        # wildly different risk for the same 'quantity'.
        if not price or price <= 0:
            return 0, f'{symbol or instrument_key}: no price, cannot size equity'
        notional = settings.EQUITY_MARGIN_PER_TRADE * settings.EQUITY_LEVERAGE
        qty = int(notional // price)
        if qty <= 0:
            return 0, (f'{symbol}: Rs {notional:,.0f} notional buys 0 shares '
                       f'at {price:,.2f}')
        if settings.MAX_NOTIONAL_PER_TRADE:
            notional = min(notional, settings.MAX_NOTIONAL_PER_TRADE)
            qty = int(notional // price)
            if qty <= 0:
                return 0, (f'{symbol}: notional cap Rs '
                           f'{settings.MAX_NOTIONAL_PER_TRADE:,.0f} buys 0 shares '
                           f'at {price:,.2f}')
        qty = min(qty, settings.EQUITY_MAX_QTY)
        return qty, (f'Rs {notional:,.0f} / {price:,.2f} = {qty}')

    if settings.MIN_PREMIUM and price is not None and 0 < price < settings.MIN_PREMIUM:
        return 0, (f'{symbol or instrument_key}: premium {price:,.2f} below '
                   f'MIN_PREMIUM {settings.MIN_PREMIUM:,.2f}')

    u = underlying_of(instrument_key, symbol)
    lots = settings.LOTS_BY_UNDERLYING.get(u)
    ls = lot_size_of(instrument_key)
    if lots and ls:
        note = f'{lots} lot x {ls}'
        if settings.MAX_NOTIONAL_PER_TRADE and price:
            # Options trade in whole lots, so the cap must round DOWN to a lot
            # boundary. Scaling continuously would report a quantity that cannot
            # be sent to the exchange, and 0 lots means the cap says do not trade.
            afford = int(settings.MAX_NOTIONAL_PER_TRADE // (ls * price))
            if afford < lots:
                if afford <= 0:
                    return 0, (f'{symbol or u}: 1 lot = Rs {ls * price:,.0f} exceeds '
                               f'cap Rs {settings.MAX_NOTIONAL_PER_TRADE:,.0f}')
                note = (f'{afford} lot x {ls} (capped from {lots}; '
                        f'Rs {afford * ls * price:,.0f})')
                lots = afford
        return lots * ls, f'{note} = {lots * ls}'
    if lots and not ls:
        return 0, (f'{u}: lot_size missing from the instrument master — refusing to '
                   f'size (is data/{instrument_key.split("_")[0]}.json current?)')
    return 0, (f'{u or "?"}: no LOTS_BY_UNDERLYING entry '
               f'(tried {underlying_candidates(instrument_key, symbol)})')
