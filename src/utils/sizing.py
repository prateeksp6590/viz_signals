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


def underlying_of(instrument_key: str, symbol: str = '') -> str:
    info = _master_index().get(instrument_key)
    if info and info['underlying']:
        return info['underlying']
    # fall back to the leading word of a trading symbol: 'SENSEX 77500 CE 06 AUG 26'
    m = re.match(r'^([A-Z&]+)', (symbol or '').upper())
    return m.group(1) if m else ''


def lot_size_of(instrument_key: str) -> int:
    info = _master_index().get(instrument_key)
    return info['lot_size'] if info else 0


def quantity_for(instrument_key: str, symbol: str = '') -> tuple[int, str]:
    """(quantity, explanation). Falls back to ORDER_QTY_DEFAULT when unmapped."""
    u = underlying_of(instrument_key, symbol)
    lots = settings.LOTS_BY_UNDERLYING.get(u)
    ls = lot_size_of(instrument_key)
    if lots and ls:
        return lots * ls, f'{lots} lot x {ls} = {lots * ls}'
    if lots and not ls:
        return settings.ORDER_QTY_DEFAULT, f'{u}: lot_size unknown (master missing?)'
    return settings.ORDER_QTY_DEFAULT, f'{u or "?"}: no LOTS_BY_UNDERLYING entry'
