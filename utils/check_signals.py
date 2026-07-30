"""
viz_signals signal healthcheck — the counterpart to viz_hedge/utils/check_feed.py.

Reads the local JSONL journal (authoritative: it records EVERY raw signal, including
ones the engine then suppressed) and cross-checks it against the InfluxDB `signals`
bucket (what a dashboard sees). Divergence between the two means the Influx sink is
failing silently.

Usage:
  python utils/check_signals.py                   # today
  python utils/check_signals.py --date 20260731
  python utils/check_signals.py --influx-only     # e.g. from your laptop
  python utils/check_signals.py --tail 20         # last N signals, chronological
  python utils/check_signals.py --stale 1800      # WARN if nothing for N seconds

Exit codes (for cron/alerting):
  0 = OK      1 = WARNING (none / stale / sinks disagree)      2 = CRITICAL
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

IST = timezone(timedelta(hours=5, minutes=30))
ENGINE_START, ENGINE_STOP = dt_time(9, 16), dt_time(15, 30)
VERDICT = ['OK', 'WARNING', 'CRITICAL']


def _age(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f'{sec}s'
    if sec < 3600:
        return f'{sec // 60}m{sec % 60:02d}s'
    return f'{sec // 3600}h{(sec % 3600) // 60:02d}m'


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _ts(rec: dict):
    v = rec.get('ts') or rec.get('entry_ts')
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)).astimezone(IST)
    except ValueError:
        return None


def from_influx(date_str: str, bucket: str):
    """(count, per-symbol, newest ts, error)."""
    try:
        from influxdb_client import InfluxDBClient
    except ImportError:
        return 0, {}, None, 'influxdb-client not installed'
    url, tok, org = os.getenv('INFLUX_URL'), os.getenv('INFLUX_TOKEN'), os.getenv('INFLUX_ORG')
    if not all([url, tok, org]):
        return 0, {}, None, 'INFLUX_URL/TOKEN/ORG not set'
    day = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=IST)
    flux = (f'from(bucket:"{bucket}")\n'
            f'  |> range(start: {day.date()}T00:00:00+05:30, '
            f'stop: {(day + timedelta(days=1)).date()}T00:00:00+05:30)\n'
            f'  |> filter(fn: (r) => r._measurement == "signal" and r._field == "price")')
    try:
        with InfluxDBClient(url=url, token=tok, org=org, timeout=60_000) as c:
            tables = c.query_api().query(flux, org=org)
    except Exception as e:
        msg = ' '.join(str(e).split())
        if 'could not find bucket' in msg or 'not found' in msg.lower():
            return 0, {}, None, f'NO_BUCKET:{bucket}'
        return 0, {}, None, (msg[:160] + '…') if len(msg) > 160 else msg
    per, newest, n = Counter(), None, 0
    for t in tables:
        for r in t.records:
            n += 1
            per[r.values.get('symbol', '?')] += 1
            ts = r.get_time().astimezone(IST)
            newest = ts if newest is None else max(newest, ts)
    return n, dict(per), newest, None


def main() -> int:
    ap = argparse.ArgumentParser(description='viz_signals signal healthcheck')
    ap.add_argument('--date', help='YYYYMMDD (default: today IST)')
    ap.add_argument('--tail', type=int, default=0)
    ap.add_argument('--stale', type=int, default=1800)
    ap.add_argument('--influx-only', action='store_true')
    args = ap.parse_args()

    now = datetime.now(IST)
    date_str = args.date or now.strftime('%Y%m%d')
    bucket = os.getenv('SIGNALS_BUCKET', 'signals')
    jdir = Path(os.getenv('JOURNAL_DIR', str(ROOT / 'journal'))) / date_str
    live = (ENGINE_START <= now.time() <= ENGINE_STOP) and date_str == now.strftime('%Y%m%d')

    print(f'\nviz_signals — {date_str}   (now {now:%H:%M:%S} IST'
          f"{', ENGINE HOURS' if live else ', outside engine hours'})")
    print(f'journal: {jdir}    influx bucket: {bucket}\n')

    rc, notes = 0, []
    sigs = [] if args.influx_only else _read_jsonl(jdir / 'signals.jsonl')
    rejs = [] if args.influx_only else _read_jsonl(jdir / 'rejections.jsonl')
    poss = [] if args.influx_only else _read_jsonl(jdir / 'positions.jsonl')
    fills = [] if args.influx_only else _read_jsonl(jdir / 'fills.jsonl')

    if not args.influx_only and not jdir.exists():
        print(f'  No journal directory for {date_str} — the engine has not run today.')
        if live:
            print('    during engine hours this means it is not running. Check:')
            print('      systemctl list-timers vizsignals.timer')
            print('      journalctl -u vizsignals -n 50 --no-pager')
            print('    to start it for the rest of today:  sudo systemctl start vizsignals')
        rc = 2 if live else 1

    if sigs:
        by = defaultdict(list)
        for s in sigs:
            by[s.get('symbol', '?')].append(s)
        print(f"  {'INSTRUMENT':<34}{'SIG':>5}{'LONG':>6}{'SHORT':>6}{'EXIT':>6}"
              f"  {'LAST':<10}{'ANGLE med/max':>15}")
        print('  ' + '-' * 86)
        for sym in sorted(by):
            rows = by[sym]
            acts = Counter(r.get('action') for r in rows)
            angs = sorted(float(r['meta']['angle_deg']) for r in rows
                          if isinstance(r.get('meta'), dict) and 'angle_deg' in r['meta'])
            last = max((t for t in (_ts(r) for r in rows) if t), default=None)
            amed = f'{angs[len(angs) // 2]:.2f}/{angs[-1]:.2f}' if angs else '-'
            print(f'  {sym[:34]:<34}{len(rows):>5}{acts.get("ENTER_LONG", 0):>6}'
                  f'{acts.get("ENTER_SHORT", 0):>6}{acts.get("EXIT", 0):>6}'
                  f"  {last.strftime('%H:%M:%S') if last else '-':<10}{amed:>15}")
        print('  ' + '-' * 86)

    newest_local = max((t for t in (_ts(r) for r in sigs) if t), default=None)
    line = f'\n  raw signals (journal) : {len(sigs)}'
    if newest_local:
        line += (f'   newest {newest_local:%H:%M:%S} IST '
                 f'({_age((now - newest_local).total_seconds())} ago)')
    print(line)
    if rejs:
        print(f'  suppressed            : {len(rejs)}  '
              f'{dict(Counter(r.get("reject_reason", "?") for r in rejs))}')
    if fills:
        print(f'  fills                 : {len(fills)}')
    if poss:
        closed = [p for p in poss if p.get('event') == 'close' and p.get('realized_pnl') is not None]
        opened = [p for p in poss if p.get('event') == 'open']
        print(f'  positions             : {len(opened)} opened, {len(closed)} closed')
        if closed:
            pnl = sum(float(p['realized_pnl']) for p in closed)
            wins = sum(1 for p in closed if float(p['realized_pnl']) > 0)
            print(f'  realised P&L          : {pnl:+.2f}   '
                  f'win rate {100 * wins / len(closed):.0f}% ({wins}/{len(closed)})')

    n_inf, _per, newest_inf, err = from_influx(date_str, bucket)
    if err and err.startswith('NO_BUCKET:'):
        print(f'\n  influx signals        : bucket "{bucket}" does not exist yet')
        notes.append(f'the "{bucket}" bucket is created by the engine on its first run '
                     f'(Journal._ensure_bucket) — expected before then')
    elif err:
        print(f'\n  influx signals        : query failed — {err}')
        rc = max(rc, 1)
        notes.append(f'InfluxDB signal query failed: {err}')
    else:
        print(f'  influx signals        : {n_inf}'
              + (f'   newest {newest_inf:%H:%M:%S} IST' if newest_inf else ''))
        if sigs and n_inf == 0:
            rc = max(rc, 1)
            notes.append('journal has signals but InfluxDB has none — the Influx sink is '
                         'failing (check PERSIST_TO_INFLUX and the service log)')
        elif sigs and abs(n_inf - len(sigs)) > max(2, 0.05 * len(sigs)):
            rc = max(rc, 1)
            notes.append(f'sink mismatch: journal {len(sigs)} vs influx {n_inf}')

    if args.tail and sigs:
        print(f'\n  last {min(args.tail, len(sigs))} signals:')
        for s in sigs[-args.tail:]:
            t = _ts(s)
            m = s.get('meta') or {}
            a, th = m.get('angle_deg'), m.get('threshold_deg')
            print(f"    {t.strftime('%H:%M:%S') if t else '-':9} {s.get('action', ''):<12}"
                  f"{str(s.get('symbol', ''))[:32]:<32} @ {s.get('price', '?'):>9}"
                  + (f'  angle {a:.2f} >= {th:.2f}' if a is not None and th is not None else ''))

    total = len(sigs) if not args.influx_only else n_inf
    if total == 0:
        rc = max(rc, 1)
        notes.append('no signals yet during engine hours — normal early on (the adaptive '
                     'threshold needs ~1,081 ticks of warm-up), suspicious after ~10:00'
                     if live else 'no signals for this day')
    elif live and newest_local:
        age = (now - newest_local).total_seconds()
        if age > args.stale:
            rc = max(rc, 1)
            notes.append(f'newest signal {_age(age)} ago (> {args.stale}s) during engine hours')

    print(f'\nRESULT: {VERDICT[rc]}')
    for n in notes:
        print(f'  - {n}')
    print()
    return rc


if __name__ == '__main__':
    sys.exit(main())
