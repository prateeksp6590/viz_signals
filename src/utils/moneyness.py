"""
Classify option legs as ITM / ATM / OTM from live premiums alone.

Why it matters (measured 2026-08-04): the ITM legs were the entire day's loss.
SENSEX ITM puts at premiums of 367-746 lost 6,308 / 3,848 / 3,238 while the OTM
calls at premiums of 136-163 made +4,256 / +3,090. Mechanically: an ITM option has
low gamma relative to its premium, so the same curvature signal produces a small
PERCENTAGE move but a large RUPEE loss at fixed lot size. They are also the thin
legs — SENSEX 79400 PE traded 2,346 times against 17,000+ on the OTM side, so the
spread is wider exactly where the ticket is biggest.

ATM is found from the data, not from a spot feed: it is the strike where the call
and put premiums are closest. That self-corrects as spot moves during the day and
needs no extra API call.
"""

import re

# 'NIFTY 24550 CE 04 AUG 26' / 'SENSEX 79400 PE 06 AUG 26' / 'CRUDEOILM 7750 CE 17 AUG 26'
_SYM = re.compile(r'^(?P<und>[A-Z&]+)\s+(?P<strike>[\d.]+)\s+(?P<typ>CE|PE)\s+(?P<exp>.+)$')


def parse(symbol: str):
    m = _SYM.match((symbol or '').strip().upper())
    if not m:
        return None
    return {'underlying': m['und'], 'strike': float(m['strike']),
            'type': m['typ'], 'expiry': m['exp'].strip()}


def classify(legs: dict[str, tuple[str, float]]) -> dict[str, str]:
    """{key: (symbol, ltp)} -> {key: 'ITM' | 'ATM' | 'OTM' | 'UNKNOWN'}.

    Groups by (underlying, expiry) and finds the strike whose CE and PE premiums are
    closest — that is ATM. Anything needing both legs falls back to UNKNOWN, which
    callers should treat as tradeable rather than silently dropping it.
    """
    groups: dict[tuple, dict] = {}
    meta: dict[str, dict] = {}
    for key, (sym, ltp) in legs.items():
        p = parse(sym)
        if not p or ltp is None:
            continue
        meta[key] = p
        g = groups.setdefault((p['underlying'], p['expiry']), {})
        g.setdefault(p['strike'], {})[p['type']] = ltp

    atm: dict[tuple, float] = {}
    for gk, strikes in groups.items():
        paired = {s: v for s, v in strikes.items() if 'CE' in v and 'PE' in v}
        if paired:
            atm[gk] = min(paired, key=lambda s: abs(paired[s]['CE'] - paired[s]['PE']))
        elif strikes:
            atm[gk] = sorted(strikes)[len(strikes) // 2]

    out = {}
    for key, p in meta.items():
        gk = (p['underlying'], p['expiry'])
        a = atm.get(gk)
        if a is None:
            out[key] = 'UNKNOWN'
            continue
        if p['strike'] == a:
            out[key] = 'ATM'
        elif (p['type'] == 'CE' and p['strike'] < a) or (p['type'] == 'PE' and p['strike'] > a):
            out[key] = 'ITM'
        else:
            out[key] = 'OTM'
    for key in legs:
        out.setdefault(key, 'UNKNOWN')
    return out
