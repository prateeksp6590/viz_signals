#!/usr/bin/env python3
"""Market state dashboard — bearish/bullish orientation, no signals.

    python utils/market_state.py                 # terminal
    python utils/market_state.py --html out.html # + a self-contained page

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
It shows STATE, not triggers. There is no "buy" or "sell" anywhere in the output and
there should never be one. Every automated trigger this project produced was measured
and found to be worth nothing; the honest use of this data is orientation before a
decision you make yourself.

It is also deliberately not a service. vizapi was OOM-killed on this box and taken
down; this is a script that prints, optionally writing one self-contained HTML file
with no server, no React and no CDN. Run it on demand or from a timer.

TWO DATA SOURCES, ON PURPOSE
----------------------------
  REST (Upstox)  index levels, MCX, VIX and all trend/percentile history.
                 Independent of the feeder, so it works on a day the feeder died —
                 which is the day you most want to look at a dashboard.
  InfluxDB       option positioning (PCR, OI concentration, IV skew), because the
                 tick store already holds oi/iv per strike with a schema verified in
                 this repo, whereas the option-chain REST response shape is not
                 something I could confirm. Building against an unverified shape
                 would produce a number that looks fine and is wrong.

Either source can fail without taking the other down; each section says so plainly
rather than printing a blank.

READING IT
----------
The VIX row carries a threshold from an actual measurement: vix_premium.py found the
17.4+ bucket had the smallest variance premium (+1.55 vs +3.35) and every
catastrophic window (-40.94). That is the one number here with a study behind it.
Everything else is orientation.
"""

import argparse
import html as _html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import REPO_ROOT                             # noqa: E402,F401

from src.config import settings                              # noqa: E402
from poll_ohlc import load_token                             # noqa: E402
from backfill_ohlc import find_master_dir                    # noqa: E402
from vix_premium import fetch_daily                          # noqa: E402
from multiday_paper import quote                             # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
VIX_KEY = 'NSE_INDEX|India VIX'
VIX_CAUTION = 17.4          # measured: see module docstring
INDICES = [('NIFTY 50', 'NSE_INDEX|Nifty 50'), ('SENSEX', 'BSE_INDEX|SENSEX')]
MCX = ['CRUDEOILM', 'GOLDM', 'SILVERM', 'NATURALGAS']


def mcx_front(master_dir: Path, names) -> list:
    """Nearest-dated MCX future per commodity -> [(name, symbol, key)].

    MCX trading-symbol formats vary, so this matches on the commodity name and picks
    the earliest parseable expiry at or after today, falling back to the first match
    if no date parses. The chosen SYMBOL is printed so a wrong pick is visible rather
    than silent.
    """
    today = datetime.now(IST).date()
    found = {n: [] for n in names}
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
            key = str(r.get('instrument_key') or '')
            if not key.startswith('MCX'):
                continue
            sym = str(r.get('trading_symbol') or '').upper()
            # FUTURES ONLY. Matching on the commodity name alone also catches
            # options — the first version returned "CRUDEOILM 11200 CE 17 SEP" at
            # 0.00, an illiquid far strike, and presented it as the front-month
            # future. Same trap generate_option_chain.py documents: futures carry no
            # strike, so an option filter must not be reused for them.
            itype = str(r.get('instrument_type') or '').upper()
            if itype and not itype.startswith('FUT'):
                continue
            if not itype and (' CE ' in f' {sym} ' or ' PE ' in f' {sym} '):
                continue
            try:
                if float(r.get('strike_price') or 0) > 0:
                    continue
            except (TypeError, ValueError):
                pass
            for n in names:
                if sym.startswith(n):
                    exp = None
                    for fmt in ('%d %b %y', '%d %b %Y', '%d%b%y'):
                        for chunk in (' '.join(sym.split()[1:4]), sym[len(n):].strip()):
                            try:
                                exp = datetime.strptime(chunk.title(), fmt).date()
                                break
                            except ValueError:
                                continue
                        if exp:
                            break
                    found[n].append((exp, sym, key))
    out = []
    for n in names:
        c = [x for x in found[n] if x[0] and x[0] >= today] or found[n]
        if c:
            c.sort(key=lambda x: (x[0] is None, x[0]))
            out.append((n, c[0][1], c[0][2]))
    return out


# MA separation must exceed this many daily standard deviations to earn a label.
# CHOSEN FROM A MEASURED TRADE-OFF, not picked by feel. 300 pure random walks and
# 300 genuine uptrends (+0.3%/day drift against 1% daily noise):
#
#   BIAS_SD   noise correctly "mixed"   real trend still detected
#     0.50            49%                       86%
#     1.00            63%                       78%
#     1.50            74%                       74%      <- chosen
#     2.00            81%                       63%
#
# 1.5 is the crossover. Erring toward "mixed" is the right side to be wrong on: a
# missed trend costs one skipped trade, a confidently mislabelled random market costs
# a position taken for a reason that was never there.
BIAS_SD = 1.5


def trend_block(px: pd.Series) -> dict:
    """Orientation only: where price sits relative to its own recent history.

    THE BIAS LABEL IS DELIBERATELY HARD TO EARN. A naive `last > ma20 > ma50` is
    close to a coin flip on a directionless series — tested on pure noise it returned
    "bullish", because two moving averages of the same random walk sit in *some*
    order and the order means nothing when they are 0.1% apart.

    So the separation must exceed BIAS_SD times the series' own daily volatility
    before anything but "mixed" is printed. On an index with ~1% daily sd a real
    trend separates the averages by 2-3%; noise separates them by ~0.2%. A dashboard
    that confidently labels noise is worse than one that admits it does not know.
    """
    if len(px) < 60:
        return {}
    last = float(px.iloc[-1])
    ma20, ma50 = float(px.iloc[-20:].mean()), float(px.iloc[-50:].mean())
    hi20, lo20 = float(px.iloc[-20:].max()), float(px.iloc[-20:].min())
    pos = 100.0 * (last - lo20) / (hi20 - lo20) if hi20 > lo20 else np.nan

    sd = float(np.std(np.diff(np.log(px.values[-60:])), ddof=1))
    gap = BIAS_SD * sd
    sep_ma = (ma20 - ma50) / ma50 if ma50 else 0.0
    sep_px = (last - ma20) / ma20 if ma20 else 0.0
    if sep_ma > gap and sep_px > gap:
        bias = 'bullish'
    elif sep_ma < -gap and sep_px < -gap:
        bias = 'bearish'
    else:
        bias = 'mixed'

    return {'last': last,
            'r1': 100.0 * (last / float(px.iloc[-2]) - 1),
            'r5': 100.0 * (last / float(px.iloc[-6]) - 1),
            'r20': 100.0 * (last / float(px.iloc[-21]) - 1),
            'vs_ma20': 100.0 * sep_px,
            'vs_ma50': 100.0 * (last / ma50 - 1),
            'range_pos': pos,
            'daily_sd': 100.0 * sd,
            'bias': bias}


def option_state(date_str: str, segment: str, underlying: str) -> dict:
    """PCR, OI concentration and IV skew from the tick store.

    Uses the LAST oi/iv per measurement for the day. Returns {} on any failure so a
    dead feeder degrades this section only.
    """
    try:
        from influxdb_client import InfluxDBClient
    except ImportError:
        return {'error': 'influxdb-client not installed'}
    d = datetime.strptime(date_str, '%Y%m%d').date()
    q = (f'from(bucket: "{settings.INFLUX_BUCKET}")\n'
         f'  |> range(start: {d}T00:00:00+05:30, '
         f'stop: {d + timedelta(days=1)}T00:00:00+05:30)\n'
         f'  |> filter(fn: (r) => r.segment == "{segment}")\n'
         f'  |> filter(fn: (r) => r._field == "oi" or r._field == "iv" '
         f'or r._field == "ltp")\n'
         f'  |> group(columns: ["_measurement", "_field"])\n'
         f'  |> last()\n'
         f'  |> keep(columns: ["_measurement", "_field", "_value"])')
    try:
        with InfluxDBClient(url=settings.INFLUX_URL, token=settings.INFLUX_TOKEN,
                            org=settings.INFLUX_ORG, timeout=120_000) as c:
            tables = c.query_api().query(q)
    except Exception as e:
        return {'error': ' '.join(str(e).split())[:160]}

    rows = {}
    for t in tables:
        for r in t.records:
            m = str(r.values.get('_measurement') or '')
            parts = m.split()
            if len(parts) < 4 or parts[-4] not in ('CE', 'PE'):
                continue
            if underlying.upper() not in m.upper():
                continue
            try:
                strike = float(parts[-5])
            except (ValueError, IndexError):
                continue
            rows.setdefault((strike, parts[-4]), {})[r.values.get('_field')] = \
                r.get_value()
    if not rows:
        return {'error': f'no {segment} option data for {date_str}'}

    ce_oi = sum(v.get('oi') or 0 for (s, o), v in rows.items() if o == 'CE')
    pe_oi = sum(v.get('oi') or 0 for (s, o), v in rows.items() if o == 'PE')
    by_strike = {}
    for (s, o), v in rows.items():
        by_strike.setdefault(s, {})[o] = v
    atm = min((s for s in by_strike
               if 'CE' in by_strike[s] and 'PE' in by_strike[s]
               and by_strike[s]['CE'].get('ltp') and by_strike[s]['PE'].get('ltp')),
              key=lambda s: abs((by_strike[s]['CE']['ltp'])
                                - (by_strike[s]['PE']['ltp'])), default=None)
    top_ce = sorted(((v.get('oi') or 0, s) for (s, o), v in rows.items() if o == 'CE'),
                    reverse=True)[:3]
    top_pe = sorted(((v.get('oi') or 0, s) for (s, o), v in rows.items() if o == 'PE'),
                    reverse=True)[:3]
    ce_iv = [v['iv'] for (s, o), v in rows.items() if o == 'CE' and v.get('iv')]
    pe_iv = [v['iv'] for (s, o), v in rows.items() if o == 'PE' and v.get('iv')]
    return {'pcr': (pe_oi / ce_oi) if ce_oi else np.nan,
            'ce_oi': ce_oi, 'pe_oi': pe_oi, 'atm': atm,
            'top_ce': top_ce, 'top_pe': top_pe,
            'ce_iv': float(np.median(ce_iv)) if ce_iv else np.nan,
            'pe_iv': float(np.median(pe_iv)) if pe_iv else np.nan,
            'strikes': len(by_strike)}


NARROW_W = 34


def render_narrow(now, vix_now, pct, trends, mcx_rows, opt, underlying) -> str:
    """A phone-shaped layout, ~34 chars wide.

    The 78-column table is unreadable on Telegram: <pre> does not scroll on the
    Android client, so every row soft-wraps mid-number and a header lands two lines
    away from its data. Rather than shrink the columns, the values go vertical —
    one instrument per block, labels beside numbers instead of above them.
    """
    L = []
    L.append('MARKET STATE')
    L.append(f'{now:%d %b %H:%M} IST')
    L.append('-' * NARROW_W)

    if vix_now is None:
        L.append('VIX  unavailable')
    else:
        L.append(f'VIX {vix_now:6.2f}   {pct:.0f}th pct')
        L.append('  ABOVE 17.4 - do not sell' if vix_now >= VIX_CAUTION
                 else '  below 17.4 - in range')
    L.append('')

    for name, t in trends.items():
        L.append(f'{name[:10]:<10}{t["last"]:>12,.1f}')
        L.append(f'  1d {t["r1"]:+6.2f}  5d {t["r5"]:+6.2f}')
        L.append(f'  20d{t["r20"]:+6.2f}  MA20{t["vs_ma20"]:+6.2f}')
        L.append(f'  range {t["range_pos"]:.0f}%   {t["bias"].upper()}')
        L.append('')

    if mcx_rows:
        L.append('MCX front-month')
        for nm, sym, v in mcx_rows:
            L.append(f'  {nm[:11]:<11}'
                     + (f'{v:>13,.1f}' if v is not None else f'{"no quote":>13}'))
        L.append('  (API trading disabled)')
        L.append('')

    L.append(f'{underlying} OPTIONS')
    if opt.get('error'):
        L.append(f'  n/a: {str(opt["error"])[:26]}')
    else:
        L.append(f'  PCR {opt["pcr"]:.2f}'
                 + (f'   ATM {opt["atm"]:,.0f}' if opt.get('atm') else ''))
        if np.isfinite(opt.get('ce_iv', np.nan)):
            L.append(f'  IV  CE {opt["ce_iv"]:.1f}  PE {opt["pe_iv"]:.1f}')
            L.append(f'  skew {opt["pe_iv"] - opt["ce_iv"]:+.2f}')
        if opt.get('top_ce'):
            L.append(f'  CE wall {opt["top_ce"][0][1]:,.0f}')
        if opt.get('top_pe'):
            L.append(f'  PE wall {opt["top_pe"][0][1]:,.0f}')
    L.append('-' * NARROW_W)
    L.append('state, not signals')
    return '\n'.join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--html', help='also write a self-contained HTML page here')
    ap.add_argument('--years', type=float, default=5.0, help='history for percentiles')
    ap.add_argument('--date', default=datetime.now(IST).strftime('%Y%m%d'))
    ap.add_argument('--segment', default='BSE_FO')
    ap.add_argument('--underlying', default='SENSEX')
    ap.add_argument('--master-dir', default=None)
    ap.add_argument('--narrow', action='store_true',
                    help='~34-char vertical layout for phones (Telegram wraps the '
                         'wide one into unreadable soft-wrapped rows)')
    a = ap.parse_args()

    token = load_token()
    sess = requests.Session()
    d_to = datetime.now(IST).date()
    d_from = d_to - timedelta(days=int(365 * a.years) + 10)
    now = datetime.now(IST)
    out = [f'MARKET STATE   {now:%Y-%m-%d %H:%M:%S IST}', '=' * 78]

    # ---- VIX -------------------------------------------------------------
    vix_txt, vix_now, pct = [], None, None
    try:
        v = fetch_daily(VIX_KEY, d_from, d_to, token, sess)
        vix_now = float(v.iloc[-1])
        pct = 100.0 * float((v.values < vix_now).mean())
        p1 = v.iloc[-250:] if len(v) > 250 else v
        zone = ('CAUTION — smallest premium, every worst window'
                if vix_now >= VIX_CAUTION else 'inside the measured-favourable range')
        vix_txt = [f'INDIA VIX   {vix_now:6.2f}   {pct:3.0f}th pct of {len(v)} sessions'
                   f'   (1y range {p1.min():.1f}-{p1.max():.1f})',
                   f'            threshold {VIX_CAUTION} -> {zone}']
    except Exception as e:
        vix_txt = [f'INDIA VIX   unavailable: {str(e)[:90]}']
    out += [''] + vix_txt

    # ---- indices ---------------------------------------------------------
    out += ['', 'INDEX TREND', '-' * 78,
            f"  {'index':<12}{'last':>11}{'1d%':>7}{'5d%':>7}{'20d%':>7}"
            f"{'vsMA20':>8}{'vsMA50':>8}{'20d range':>11}{'bias':>9}"]
    trends = {}
    for name, key in INDICES:
        try:
            px = fetch_daily(key, d_from, d_to, token, sess)
            t = trend_block(px)
            if not t:
                out.append(f'  {name:<12}  only {len(px)} sessions returned')
                continue
            trends[name] = t
            out.append(f'  {name:<12}{t["last"]:>11,.1f}{t["r1"]:>7.2f}'
                       f'{t["r5"]:>7.2f}{t["r20"]:>7.2f}{t["vs_ma20"]:>8.2f}'
                       f'{t["vs_ma50"]:>8.2f}{t["range_pos"]:>10.0f}%{t["bias"]:>9}')
        except Exception as e:
            out.append(f'  {name:<12}  unavailable: {str(e)[:60]}')
    out.append('  20d range: 0% = at the 20-day low, 100% = at the 20-day high')

    # ---- MCX -------------------------------------------------------------
    out += ['', 'MCX FRONT-MONTH', '-' * 78]
    mcx_rows = []
    mdir = find_master_dir(a.master_dir)
    if not mdir:
        out.append('  instrument master not found — pass --master-dir')
    else:
        legs = mcx_front(mdir, MCX)
        if not legs:
            out.append(f'  no MCX contracts matched {", ".join(MCX)} in {mdir}')
        else:
            try:
                q = quote([k for _, _, k in legs], token, sess)
            except Exception as e:
                q = {}
                out.append(f'  quotes unavailable: {str(e)[:70]}')
            for nm, sym, key in legs:
                v = q.get(key)
                mcx_rows.append((nm, sym, v))
                out.append(f'  {nm:<14}{sym[:26]:<28}'
                           + (f'{v:>12,.2f}' if v is not None else f'{"no quote":>12}'))
            out.append('  NOTE: Upstox API trading on MCX is currently disabled '
                       '(data feed unaffected)')

    # ---- options ---------------------------------------------------------
    out += ['', f'OPTION POSITIONING — {a.underlying} {a.segment} ({a.date})', '-' * 78]
    o = option_state(a.date, a.segment, a.underlying)
    if o.get('error'):
        out += [f'  unavailable: {o["error"]}',
                '  (this section reads the tick store; the rest of the page does not)']
    else:
        out += [f'  PCR (OI)      {o["pcr"]:.2f}      '
                f'CE OI {o["ce_oi"]:,.0f}   PE OI {o["pe_oi"]:,.0f}',
                f'  ATM (by C-P)  {o["atm"]:,.0f}' if o.get('atm') else '  ATM  n/a',
                f'  CE OI walls   ' + ', '.join(f'{s:,.0f} ({v:,.0f})'
                                                for v, s in o['top_ce']),
                f'  PE OI walls   ' + ', '.join(f'{s:,.0f} ({v:,.0f})'
                                                for v, s in o['top_pe']),
                f'  median IV     CE {o["ce_iv"]:.2f}   PE {o["pe_iv"]:.2f}   '
                f'skew {o["pe_iv"] - o["ce_iv"]:+.2f}'
                if np.isfinite(o.get('ce_iv', np.nan)) else '  median IV    n/a',
                f'  {o["strikes"]} strikes with data']

    out += ['', '=' * 78,
            'State, not signals. Nothing here is a trigger — see',
            'docs/shaaru-aureus-manual-trading-rulebook.md for what to do with it.']

    if a.narrow:
        text = render_narrow(now, vix_now, pct, trends, mcx_rows, o, a.underlying)
    else:
        text = '\n'.join(out)
    print(text)

    if a.html:
        colour = ('#b00' if (vix_now or 0) >= VIX_CAUTION else '#070')
        page = (
            '<!doctype html><meta charset="utf-8">'
            f'<title>Market state {now:%Y-%m-%d %H:%M}</title>'
            '<style>body{font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;'
            'background:#faf9f7;color:#1a1a1a;margin:24px;max-width:900px}'
            'h1{font-size:17px;margin:0 0 4px}.t{color:#666;font-size:12px}'
            'pre{background:#fff;border:1px solid #e3e0da;border-radius:6px;'
            'padding:16px;overflow-x:auto;white-space:pre}'
            f'.vix{{color:{colour};font-weight:600}}</style>'
            f'<h1>Market state</h1><div class="t">generated {now:%Y-%m-%d %H:%M:%S IST}'
            ' · state, not signals</div>'
            f'<pre>{_html.escape(text)}</pre>')
        Path(a.html).write_text(page, encoding='utf-8')
        print(f'\nwrote {a.html}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
