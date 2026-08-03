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


def quantity_for(instrument_key: str, symbol: str = '') -> tuple[int, str]:
    """(quantity, explanation). Returns 0 when the instrument cannot be sized.

    0 means DO NOT TRADE. Guessing a quantity is worse than skipping: an unsized
    trade still pays full brokerage and STT, so it is negative-expectancy by
    construction regardless of whether the signal was right.
    """
    u = underlying_of(instrument_key, symbol)
    lots = settings.LOTS_BY_UNDERLYING.get(u)
    ls = lot_size_of(instrument_key)
    if lots and ls:
        return lots * ls, f'{lots} lot x {ls} = {lots * ls}'
    if lots and not ls:
        return 0, (f'{u}: lot_size missing from the instrument master — refusing to '
                   f'size (is data/{instrument_key.split("_")[0]}.json current?)')
    return 0, (f'{u or "?"}: no LOTS_BY_UNDERLYING entry '
               f'(tried {underlying_candidates(instrument_key, symbol)})')
