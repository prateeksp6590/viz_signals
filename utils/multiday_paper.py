#!/usr/bin/env python3
"""Multi-day short-vol paper position: open, mark to market, close.

    python utils/multiday_paper.py open --structure condor --wing 300
    python utils/multiday_paper.py mark
    python utils/multiday_paper.py close

WHAT THIS IS FOR, STATED PLAINLY
--------------------------------
This is an OPERATIONAL rehearsal, not an edge test. One position held to one expiry
is n=1, and a short-vol position wins roughly three in four regardless of whether its
expectancy is positive. Measured in vol_premium.py on synthetic chains, a straddle
with EXACTLY ZERO expectancy by construction showed a median outcome of +Rs 61 and a
mean of -Rs 99. A single trade samples the median.

So the P&L this produces answers nothing about edge. What it does answer, and what is
genuinely worth knowing before risking money:
  - does the quote path survive overnight and across a token refresh
  - do the legs fill as a set, or does a limit-only regime leave you half-built
  - what does the margin actually block, versus what you assumed
  - does the CAS reprice at 15:35 move the position more than you expected
Use vix_premium.py for the edge question. Use this for whether the plumbing holds.

STRUCTURES
----------
  straddle  sell ATM CE + sell ATM PE                 undefined risk, largest margin
  strangle  sell CE and PE --wing points out          undefined risk
  condor    the strangle, plus long wings further out DEFINED risk

Default is `condor`. With limited capital an undefined-risk short position facing an
overnight gap is the single fastest way to lose more than the account, and the
overnight gap is exactly what a multi-day hold signs up for.

MARGIN
------
For the condor the max loss is arithmetic and is printed exactly. For straddle and
strangle the margin is SPAN + exposure and is NOT computed here — do not guess it,
call the Upstox margin API. A number invented in this file would be believed.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                             # noqa: E402,F401

from src.config import settings                              # noqa: E402
from poll_ohlc import load_token                             # noqa: E402
from backfill_ohlc import find_master_dir                    # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OHLC_URL = 'https://api.upstox.com/v3/market-quote/ohlc'
LOT = {'SENSEX': 20, 'NIFTY': 75, 'BANKNIFTY': 35}
STATE_DIR = Path(settings.JOURNAL_DIR) / 'multiday'


def chain(master_dir: Path, underlying: str, expiry: str) -> dict:
    """(strike, 'CE'|'PE') -> (instrument_key, trading_symbol) for one expiry."""
    out = {}
    for p in sorted(Path(master_dir).glob('*.json')):
        try:
            rows = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = str(r.get('trading_symbol') or '')
            parts = sym.split()
            # 'SENSEX 77500 CE 20 AUG 26'
            if len(parts) < 6 or parts[0].upper() != underlying.upper():
                continue
            if parts[2].upper() not in ('CE', 'PE'):
                continue
            if ' '.join(parts[3:6]).upper() != expiry.upper():
                continue
            try:
                out[(float(parts[1]), parts[2].upper())] = (r['instrument_key'], sym)
            except (KeyError, ValueError):
                continue
    return out


def quote(keys: list, token: str, session=None) -> dict:
    """instrument_key -> last_price. Keyed off instrument_token, NOT the response
    dict key, which is the trading symbol and will match nothing."""
    http = session or requests
    out = {}
    for i in range(0, len(keys), 100):
        r = http.get(OHLC_URL,
                     params={'instrument_key': ','.join(keys[i:i + 100]),
                             'interval': 'I1'},
                     headers={'Accept': 'application/json',
                              'Authorization': f'Bearer {token}'}, timeout=25)
        if r.status_code == 401:
            raise SystemExit('Upstox returned 401 — token expired. Regenerate it.')
        r.raise_for_status()
        for _k, node in ((r.json().get('data')) or {}).items():
            if isinstance(node, dict) and node.get('instrument_token'):
                lp = node.get('last_price')
                if lp is None:
                    lp = (node.get('prev_ohlc') or {}).get('close')
                if lp is not None:
                    out[node['instrument_token']] = float(lp)
    return out


def leg_charges(entry: float, exit_: float, qty: int, side: str) -> float:
    """Round-trip charges for ONE leg. `side` is the OPENING side.

    STT (0.0625%) is charged on the SELL, so it applies to the entry price for a
    short leg and the exit price for a long one. Stamp duty (0.003%) applies to the
    buy. Getting this backwards flatters shorts in falling markets and longs in
    rising ones — small per leg, but a condor has four of them.
    """
    sell_px = entry if side == 'SELL' else exit_
    buy_px = exit_ if side == 'SELL' else entry
    brokerage = 20.0 * 2
    stt = 0.000625 * sell_px * qty
    txn = 0.0005 * (entry + exit_) * qty
    stamp = 0.00003 * buy_px * qty
    return brokerage + stt + txn + stamp + 0.18 * (brokerage + txn)


def build(structure: str, atm: float, step: float, wing: float, ch: dict) -> list:
    """-> [{strike, side, opt, key, symbol}] or raises if a strike is missing."""
    def need(k, o):
        if (k, o) not in ch:
            raise SystemExit(f'strike {k:.0f} {o} not in the chain — pick a smaller '
                             f'--wing or check the expiry')
        key, sym = ch[(k, o)]
        return key, sym

    legs = []
    if structure == 'straddle':
        spec = [(atm, 'CE', 'SELL'), (atm, 'PE', 'SELL')]
    elif structure == 'strangle':
        spec = [(atm + wing, 'CE', 'SELL'), (atm - wing, 'PE', 'SELL')]
    elif structure == 'condor':
        spec = [(atm + wing, 'CE', 'SELL'), (atm + 2 * wing, 'CE', 'BUY'),
                (atm - wing, 'PE', 'SELL'), (atm - 2 * wing, 'PE', 'BUY')]
    else:
        raise SystemExit(f'unknown structure {structure}')
    for k, o, side in spec:
        k = round(k / step) * step
        key, sym = need(k, o)
        legs.append({'strike': k, 'opt': o, 'side': side, 'key': key, 'symbol': sym})
    return legs


def pnl(legs: list, qty: int, marks: dict) -> tuple:
    gross = chg = 0.0
    rows = []
    for l in legs:
        now = marks.get(l['key'])
        if now is None:
            continue
        sign = 1.0 if l['side'] == 'SELL' else -1.0
        g = sign * (l['entry'] - now) * qty
        c = leg_charges(l['entry'], now, qty, l['side'])
        gross += g
        chg += c
        rows.append((l, now, g, c))
    return gross, chg, rows


def dte_of(expiry: str):
    for fmt in ('%d %b %y', '%d %B %y', '%d %b %Y'):
        try:
            e = datetime.strptime(expiry.strip().title(), fmt).date()
            return (e - datetime.now(IST).date()).days
        except ValueError:
            continue
    return None


def check_dte(expiry: str, min_dte: int, force: bool) -> None:
    """Refuse a near-expiry structure unless it is asked for explicitly.

    The variance premium was measured at 5-21 day horizons (vix_premium.py:
    +2.37/+2.10/+2.02 vol points at n=5/10/21). A 1-DTE condor is not a small
    version of that trade — gamma dominates, the premium collected is tiny, and a
    move through the short strike cannot be recovered by time. Opening one and
    reading the result as evidence about the premium would compare two unrelated
    things.

    It is also the easy mistake to make: the default expiry goes stale every week,
    so whatever was current when the flag was written becomes 1 DTE a few days later.
    """
    d = dte_of(expiry)
    if d is None:
        print(f'  could not parse expiry "{expiry}" — proceeding without a DTE check')
        return
    if d < 0:
        raise SystemExit(f'"{expiry}" expired {-d} day(s) ago.')
    if d < min_dte and not force:
        raise SystemExit(
            f'\n"{expiry}" is {d} day(s) out; the premium was measured over 5-21 days.\n'
            f'At {d} DTE this is a gamma trade, not a vol-premium trade, and its P&L\n'
            f'says nothing about the research.\n'
            f'  -> pick the next expiry, or pass --force to open it anyway.')
    print(f'  {d} days to expiry')


def state_path(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f'{name}.json'


def cmd_open(a) -> int:
    mdir = find_master_dir(a.master_dir)
    if not mdir:
        return print('instrument master not found; pass --master-dir') or 1
    check_dte(a.expiry, a.min_dte, a.force)
    ch = chain(mdir, a.underlying, a.expiry)
    if not ch:
        return print(f'no {a.underlying} contracts for expiry "{a.expiry}"') or 1

    token = load_token()
    strikes = sorted({k for k, _ in ch})
    keys = [ch[(k, o)][0] for k in strikes for o in ('CE', 'PE') if (k, o) in ch]
    marks = quote(keys, token)

    # ATM by put-call parity: |C - P| is zero exactly at the forward
    best, bestd = None, None
    for k in strikes:
        c, p = ch.get((k, 'CE')), ch.get((k, 'PE'))
        if not c or not p:
            continue
        cv, pv = marks.get(c[0]), marks.get(p[0])
        if cv is None or pv is None:
            continue
        d = abs(cv - pv)
        if bestd is None or d < bestd:
            best, bestd = k, d
    if best is None:
        return print('could not locate ATM — no strike had both CE and PE quoted') or 1
    fwd = best + (marks[ch[(best, 'CE')][0]] - marks[ch[(best, 'PE')][0]])
    step = min((b - a_) for a_, b in zip(strikes, strikes[1:])) if len(strikes) > 1 else 100

    legs = build(a.structure, best, step, a.wing, ch)
    qty = a.lots * LOT.get(a.underlying.upper(), 20)
    for l in legs:
        v = marks.get(l['key'])
        if v is None:
            return print(f'no quote for {l["symbol"]} — cannot open') or 1
        l['entry'] = float(v)

    credit = sum((1 if l['side'] == 'SELL' else -1) * l['entry'] for l in legs) * qty
    st = {'name': a.name, 'opened': datetime.now(IST).isoformat(),
          'underlying': a.underlying, 'expiry': a.expiry, 'structure': a.structure,
          'atm': best, 'forward_at_entry': fwd, 'lots': a.lots, 'qty': qty,
          'wing': a.wing, 'legs': legs, 'net_credit': credit}
    state_path(a.name).write_text(json.dumps(st, indent=2), encoding='utf-8')

    print(f'\nOPENED {a.structure} on {a.underlying} {a.expiry}  '
          f'({a.lots} lot(s), qty {qty})')
    print(f'  forward at entry ~ {fwd:,.1f}   ATM strike {best:,.0f}')
    print(f"  {'leg':<32}{'side':>6}{'price':>10}")
    for l in legs:
        print(f'  {l["symbol"][:32]:<32}{l["side"]:>6}{l["entry"]:>10.2f}')
    print(f'  net credit received: Rs {credit:,.0f}')
    if a.structure == 'condor':
        width = a.wing * qty
        print(f'  MAX LOSS (defined): Rs {width - credit:,.0f}   '
              f'max gain Rs {credit:,.0f}')
        print(f'  breakeven needs the index inside '
              f'{best - a.wing - credit / qty:,.0f} .. '
              f'{best + a.wing + credit / qty:,.0f} at expiry')
    else:
        print('  MAX LOSS: UNDEFINED. Margin is SPAN + exposure — call the Upstox')
        print('  margin API for the real number; do not estimate it.')
    print(f'\n  state: {state_path(a.name)}')
    return 0


def cmd_mark(a) -> int:
    p = state_path(a.name)
    if not p.exists():
        return print(f'no open position named {a.name}') or 1
    st = json.loads(p.read_text(encoding='utf-8'))
    marks = quote([l['key'] for l in st['legs']], load_token())
    gross, chg, rows = pnl(st['legs'], st['qty'], marks)

    age = datetime.now(IST) - datetime.fromisoformat(st['opened'])
    print(f'\n{st["structure"]} on {st["underlying"]} {st["expiry"]}  '
          f'held {age.days}d {age.seconds // 3600}h')
    print(f"  {'leg':<32}{'side':>6}{'entry':>9}{'now':>9}{'P&L':>10}")
    for l, now, g, c in rows:
        print(f'  {l["symbol"][:32]:<32}{l["side"]:>6}{l["entry"]:>9.2f}'
              f'{now:>9.2f}{g:>10,.0f}')
    print(f'  {"":<32}{"":>6}{"":>9}{"GROSS":>9}{gross:>10,.0f}')
    print(f'  {"":<32}{"":>6}{"":>9}{"charges":>9}{-chg:>10,.0f}')
    print(f'  {"":<32}{"":>6}{"":>9}{"NET":>9}{gross - chg:>10,.0f}')
    if len(rows) < len(st['legs']):
        print(f'  WARNING: only {len(rows)} of {len(st["legs"])} legs quoted — '
              f'the P&L above is INCOMPLETE')
    print('\n  n=1. This says nothing about edge; see vix_premium.py for that.')
    return 0


def cmd_close(a) -> int:
    p = state_path(a.name)
    if not p.exists():
        return print(f'no open position named {a.name}') or 1
    st = json.loads(p.read_text(encoding='utf-8'))
    marks = quote([l['key'] for l in st['legs']], load_token())
    gross, chg, rows = pnl(st['legs'], st['qty'], marks)
    st['closed'] = datetime.now(IST).isoformat()
    st['exit'] = {l['key']: m for l, m, _, _ in rows}
    st['gross'], st['charges'], st['net'] = gross, chg, gross - chg
    done = STATE_DIR / f'{a.name}-closed-{datetime.now(IST):%Y%m%d%H%M}.json'
    done.write_text(json.dumps(st, indent=2), encoding='utf-8')
    p.unlink()
    print(f'\nCLOSED: gross {gross:,.0f}  charges {chg:,.0f}  NET {gross - chg:,.0f}')
    print(f'  archived to {done}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for nm in ('open', 'mark', 'close'):
        s = sub.add_parser(nm)
        s.add_argument('--name', default='current')
        if nm == 'open':
            s.add_argument('--structure', default='condor',
                           choices=['condor', 'straddle', 'strangle'])
            s.add_argument('--underlying', default='SENSEX')
            s.add_argument('--expiry', default='20 AUG 26')
            s.add_argument('--wing', type=float, default=300.0)
            s.add_argument('--lots', type=int, default=1)
            s.add_argument('--master-dir', default=None)
            s.add_argument('--min-dte', type=int, default=3,
                           help='refuse nearer expiries (premium measured at 5-21d)')
            s.add_argument('--force', action='store_true',
                           help='open inside --min-dte anyway')
    a = ap.parse_args()
    return {'open': cmd_open, 'mark': cmd_mark, 'close': cmd_close}[a.cmd](a)


if __name__ == '__main__':
    raise SystemExit(main())
