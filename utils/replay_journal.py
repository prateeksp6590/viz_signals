"""
What would today's P&L have been under a stricter entry filter?

Joins each CLOSED position back to the signal that opened it, so every trade carries
its angle/threshold ratio. Then re-totals P&L for a range of minimum ratios and
minimum expected sizes. Answers "should ANGLE_Q go up?" from your own data instead
of from a backtest on one instrument.

    python utils/replay_journal.py [--date YYYYMMDD]
"""
import argparse, json, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()
from src.config import settings                      # noqa: E402
from src.utils.sizing import quantity_for, underlying_of, lot_size_of   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
BROK, STT, TXN, SEBI, STAMP, GST = 20.0, 0.0625/100, 0.05/100, 10/1e7, 0.003/100, 0.18


def charges(entry, exit_, qty):
    b, s = entry*qty, exit_*qty
    brok = BROK*2; txn = (b+s)*TXN; stt = s*STT; sebi = (b+s)*SEBI
    stamp = b*STAMP; gst = (brok+txn+sebi)*GST
    return brok+txn+stt+sebi+stamp+gst


def read(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.now(IST).strftime('%Y%m%d'))
    a = ap.parse_args()
    d = Path(settings.JOURNAL_DIR) / a.date
    sigs = read(d / 'signals.jsonl')
    poss = [p for p in read(d / 'positions.jsonl')
            if p.get('event') == 'close' and p.get('realized_pnl') is not None]
    if not poss:
        sys.exit(f'no closed positions in {d}')

    # index entry signals per instrument by time
    by_inst = defaultdict(list)
    for s in sigs:
        if s.get('action', '').startswith('ENTER') and s.get('meta', {}).get('threshold_deg'):
            by_inst[s['instrument_key']].append(
                (datetime.fromisoformat(s['ts']), s['meta']['angle_deg'] / s['meta']['threshold_deg']))
    for v in by_inst.values():
        v.sort()

    trades = []
    for p in poss:
        key = p.get('instrument_key', '')
        ets = datetime.fromisoformat(p['entry_ts'])
        cand = [r for t, r in by_inst.get(key, []) if abs((t - ets).total_seconds()) <= 3]
        ratio = max(cand) if cand else None
        qty = int(p.get('qty') or 0) or quantity_for(key, p.get('symbol', ''))[0]
        gross = (float(p['exit_price']) - float(p['avg_entry'])) * \
                (1 if p.get('side') == 'LONG' else -1) * qty
        trades.append({'ratio': ratio, 'gross': gross,
                       'chg': charges(float(p['avg_entry']), float(p['exit_price']), qty),
                       'u': underlying_of(key, p.get('symbol', '')),
                       'hold': (datetime.fromisoformat(p['exit_ts']) - ets).total_seconds()})

    matched = [t for t in trades if t['ratio'] is not None]
    print(f'{a.date}: {len(trades)} closed trades, {len(matched)} matched to an entry signal\n')

    print('  P&L if only entries at or above a minimum signal strength were taken:')
    print(f"  {'min ratio':>10}{'trades':>8}{'win%':>7}{'gross':>12}{'charges':>10}{'NET':>12}")
    print('  ' + '-' * 61)
    for cut in (0, 1.05, 1.1, 1.2, 1.3, 1.5, 2.0, 3.0):
        sel = [t for t in matched if t['ratio'] >= cut]
        if not sel:
            continue
        g = sum(t['gross'] for t in sel); c = sum(t['chg'] for t in sel)
        w = 100*sum(1 for t in sel if t['gross'] - t['chg'] > 0)/len(sel)
        print(f'  {cut:>10.2f}{len(sel):>8}{w:>7.0f}{g:>12,.0f}{c:>10,.0f}{g-c:>12,.0f}')

    print('\n  by holding time (are the fast exits the problem?):')
    print(f"  {'hold':>12}{'trades':>8}{'win%':>7}{'avg gross':>12}{'avg chg':>10}")
    print('  ' + '-' * 50)
    for lo, hi, lbl in ((0, 30, '<30s'), (30, 120, '30-120s'), (120, 600, '2-10min'),
                        (600, 10**9, '>10min')):
        sel = [t for t in trades if lo <= t['hold'] < hi]
        if not sel:
            continue
        w = 100*sum(1 for t in sel if t['gross'] - t['chg'] > 0)/len(sel)
        print(f"  {lbl:>12}{len(sel):>8}{w:>7.0f}"
              f"{sum(t['gross'] for t in sel)/len(sel):>12,.0f}"
              f"{sum(t['chg'] for t in sel)/len(sel):>10,.0f}")

    print('\n  cost floor — gross needed per trade just to break even:')
    for u in sorted({t['u'] for t in trades if t['u']}):
        sel = [t for t in trades if t['u'] == u]
        avg = sum(t['chg'] for t in sel)/len(sel)
        beat = sum(1 for t in sel if abs(t['gross']) > avg)
        print(f'    {u:12} avg charge Rs {avg:6.0f}   '
              f'{beat}/{len(sel)} trades moved more than that')


if __name__ == '__main__':
    main()
