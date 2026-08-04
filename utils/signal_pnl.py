"""
Notional P&L for a day's signals, sized in lots.

Reads the journal written by the engine (positions.jsonl / fills.jsonl) and prices
it at `LOTS_BY_UNDERLYING` x the master's lot_size. This is a CALCULATION, not a
broker statement: fills are modelled at the signal price plus SLIPPAGE_BPS, and
charges are an estimate.

    python utils/signal_pnl.py                       # today
    python utils/signal_pnl.py --date 20260803
    python utils/signal_pnl.py --lots NIFTY:5,SENSEX:10
    python utils/signal_pnl.py --no-charges          # gross only
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from src.config import settings                       # noqa: E402
from src.utils.sizing import lot_size_of, quantity_for, underlying_of  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# Indicative charges. Equity intraday and F&O options are taxed DIFFERENTLY — STT is
# 0.025% of equity turnover on the sell side versus 0.0625% of option PREMIUM, and the
# exchange transaction charge differs by an order of magnitude. Using one model for
# both would badly misstate equity P&L, especially at 5x leverage where turnover is
# large relative to the move. Every component is shown so you can substitute your
# broker's actual slab.
GST = 0.18                       # on brokerage + txn + sebi
SEBI_FEE = 10 / 1e7

OPT = dict(brokerage=20.0, stt_sell=0.0625 / 100, txn=0.05 / 100, stamp_buy=0.003 / 100)
EQ_INTRADAY = dict(brokerage=20.0,               # or 0.03% if lower, at most brokers
                   stt_sell=0.025 / 100,         # equity intraday, sell side
                   txn=0.00297 / 100,            # NSE cash
                   stamp_buy=0.003 / 100)


def _read(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def charges(entry_px: float, exit_px: float, qty: int, equity: bool = False) -> dict:
    m = EQ_INTRADAY if equity else OPT
    buy_val, sell_val = entry_px * qty, exit_px * qty
    brok = min(m['brokerage'], 0.0003 * buy_val) + min(m['brokerage'], 0.0003 * sell_val) \
        if equity else m['brokerage'] * 2
    txn = (buy_val + sell_val) * m['txn']
    stt = sell_val * m['stt_sell']
    sebi = (buy_val + sell_val) * SEBI_FEE
    stamp = buy_val * m['stamp_buy']
    gst = (brok + txn + sebi) * GST
    return {'brokerage': brok, 'txn': txn, 'stt': stt, 'sebi': sebi,
            'stamp': stamp, 'gst': gst,
            'total': brok + txn + stt + sebi + stamp + gst}


def main() -> int:
    ap = argparse.ArgumentParser(description='notional P&L for a day of signals')
    ap.add_argument('--date', help='YYYYMMDD (default: today IST)')
    ap.add_argument('--lots', help='override, e.g. NIFTY:5,SENSEX:10')
    ap.add_argument('--no-charges', action='store_true')
    args = ap.parse_args()

    if args.lots:
        settings.LOTS_BY_UNDERLYING = {
            u.strip().upper(): int(n) for u, n in
            (p.split(':', 1) for p in args.lots.split(',') if ':' in p)}

    date_str = args.date or datetime.now(IST).strftime('%Y%m%d')
    jdir = Path(settings.JOURNAL_DIR) / date_str
    closed = [p for p in _read(jdir / 'positions.jsonl')
              if p.get('event') == 'close' and p.get('realized_pnl') is not None]

    print(f'\nNotional P&L — {date_str}   (calculation, not a broker statement)')
    print(f'journal: {jdir}')
    print(f'sizing : {settings.LOTS_BY_UNDERLYING}   '
          f'slippage {settings.SLIPPAGE_BPS} bps\n')

    if not closed:
        print('  No closed positions. Is ORDER_MODE=paper? signals_only records '
              'signals but never opens a position.\n')
        return 1

    rows, per_u = [], defaultdict(lambda: {'n': 0, 'gross': 0.0, 'chg': 0.0})
    for p in closed:
        key, sym = p.get('instrument_key', ''), p.get('symbol', '')
        entry, exit_ = float(p['avg_entry']), float(p['exit_price'])
        qty = int(p.get('qty') or 0) or quantity_for(key, sym, entry)[0]
        direction = 1 if p.get('side') == 'LONG' else -1
        # journal realized_pnl is per the qty actually tracked; re-price on `qty`
        gross = (exit_ - entry) * direction * qty
        is_eq = key.split('|', 1)[0].upper().endswith('_EQ')
        c = {'total': 0.0} if args.no_charges else charges(entry, exit_, qty, is_eq)
        u = underlying_of(key, sym)
        per_u[u]['n'] += 1
        per_u[u]['gross'] += gross
        per_u[u]['chg'] += c['total']
        rows.append({'sym': sym, 'u': u, 'side': p.get('side'), 'qty': qty,
                     'lots': qty // (lot_size_of(key) or qty or 1),
                     'entry': entry, 'exit': exit_, 'gross': gross,
                     'chg': c['total'], 'net': gross - c['total'],
                     't': p.get('exit_ts', '')[11:19]})

    print(f"  {'EXIT':<9}{'INSTRUMENT':<30}{'SIDE':<6}{'LOTS':>5}{'QTY':>7}"
          f"{'ENTRY':>9}{'EXIT':>9}{'GROSS':>11}{'CHG':>9}{'NET':>11}")
    print('  ' + '-' * 106)
    for r in sorted(rows, key=lambda r: r['t']):
        print(f"  {r['t']:<9}{r['sym'][:30]:<30}{r['side']:<6}{r['lots']:>5}{r['qty']:>7}"
              f"{r['entry']:>9.2f}{r['exit']:>9.2f}{r['gross']:>11,.2f}"
              f"{r['chg']:>9,.0f}{r['net']:>11,.2f}")
    print('  ' + '-' * 106)

    print(f"\n  {'UNDERLYING':<14}{'TRADES':>8}{'GROSS':>14}{'CHARGES':>12}{'NET':>14}")
    print('  ' + '-' * 62)
    for u in sorted(per_u):
        d = per_u[u]
        print(f"  {u:<14}{d['n']:>8}{d['gross']:>14,.2f}{d['chg']:>12,.2f}"
              f"{d['gross'] - d['chg']:>14,.2f}")
    g = sum(d['gross'] for d in per_u.values())
    c = sum(d['chg'] for d in per_u.values())
    wins = sum(1 for r in rows if r['net'] > 0)
    print('  ' + '-' * 62)
    print(f"  {'TOTAL':<14}{len(rows):>8}{g:>14,.2f}{c:>12,.2f}{g - c:>14,.2f}")
    print(f"\n  win rate {100 * wins / len(rows):.0f}% ({wins}/{len(rows)})   "
          f"charges are {100 * c / abs(g) if g else 0:.1f}% of gross")
    if not args.no_charges:
        print('  options : Rs20/order, 0.0625% STT on sell premium, 0.05% txn, '
              '0.003% stamp (buy), 18% GST')
        print('  equity  : Rs20 or 0.03% per order (lower), 0.025% STT on sell turnover, '
              '0.00297% txn, 0.003% stamp (buy), 18% GST')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
