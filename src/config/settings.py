"""All configuration, env-driven. See docs §11 for the reference table."""

import os
import re
from pathlib import Path


def _env(key: str, default=None):
    """os.getenv, but tolerant of trailing '# comments'.

    systemd's EnvironmentFile= does NOT strip inline comments the way python-dotenv
    does: it hands over the whole rest of the line. So a .env that works when loaded
    by dotenv (preflight, CLI) crashes under systemd with e.g.
        ValueError: invalid literal for int(): '60   # must exceed ANGLE_N2 ticks'
    Stripping here makes both load paths behave identically.
    """
    v = os.getenv(key)
    if v is None:
        return default
    v = re.sub(r'\s+#.*$', '', v).strip().strip('\'"')
    return v if v != '' else default

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── InfluxDB (same instance viz_hedge writes to) ──────────────────────────────
INFLUX_URL   = _env('INFLUX_URL', 'http://localhost:8086')
INFLUX_TOKEN = _env('INFLUX_TOKEN')
INFLUX_ORG   = _env('INFLUX_ORG')
# viz_hedge writes every day into ONE bucket with dateless measurements
# ({EXCH}_{trading_symbol}); the timestamp separates days. Per-day buckets are
# retired -- they hit InfluxDB Cloud's bucket-count quota.
INFLUX_BUCKET  = _env('INFLUX_BUCKET', 'tick_data')
SIGNALS_BUCKET = _env('SIGNALS_BUCKET', 'signals')

# Query shaping. A single pivoted query over every instrument x a long window is
# enough to blow the HTTP read timeout: 6 instruments x one session was ~186k rows
# and timed out at 60s, so a cold start over 79 instruments x LOOKBACK_MINUTES
# (~370k rows) certainly would. Chunk the instruments, and pull only the fields the
# strategy actually reads -- InfluxDB bills per byte scanned as well as per query.
INFLUX_QUERY_CHUNK = int(_env('INFLUX_QUERY_CHUNK', '20'))   # instruments/query
INFLUX_QUERY_TIMEOUT_MS = int(_env('INFLUX_QUERY_TIMEOUT_MS', '120000'))
# Chunks run concurrently: EC2 is ap-south-1 and InfluxDB Cloud us-east-1, so each
# query pays ~4s of transcontinental round trip regardless of how little it returns.
INFLUX_QUERY_WORKERS = int(_env('INFLUX_QUERY_WORKERS', '4'))
INFLUX_FIELDS = [f.strip() for f in _env('INFLUX_FIELDS', 'ltp,vtt,oi').split(',')
                 if f.strip()]                                    # empty = every field

# ── Instruments ───────────────────────────────────────────────────────────────
def _instruments_from_feeder(path: Path) -> list[str]:
    """Read SUBSCRIBE_INSTRUMENTS out of the feeder's .env.

    The feeder regenerates its chain every morning at ~09:09 (ATM moves, weeklies
    expire). A static ANALYZE_INSTRUMENTS here goes stale the moment an expiry rolls:
    yesterday's '30 JUL' strikes simply stop existing, and the engine would sit
    polling measurements that will never tick again -- silently, since no data is
    not an error. Inheriting keeps the two in lockstep with no extra plumbing.
    """
    try:
        for line in path.read_text().splitlines():
            if line.startswith('SUBSCRIBE_INSTRUMENTS='):
                v = re.sub(r'\s+#.*$', '', line.split('=', 1)[1]).strip()
                return [k.strip() for k in v.split(',') if k.strip()]
    except Exception:
        pass
    return []


ANALYZE_INSTRUMENTS = [
    k.strip() for k in _env('ANALYZE_INSTRUMENTS', '').split(',') if k.strip()
]
# Fall back to (or explicitly follow) the feeder's live subscription list.
FEEDER_ENV = Path(_env('FEEDER_ENV', str(REPO_ROOT.parent / 'viz_hedge' / '.env')))
FOLLOW_FEEDER = _env('FOLLOW_FEEDER', 'auto')        # auto | always | never
if FOLLOW_FEEDER == 'always' or (FOLLOW_FEEDER == 'auto' and not ANALYZE_INSTRUMENTS):
    inherited = _instruments_from_feeder(FEEDER_ENV)
    if inherited:
        ANALYZE_INSTRUMENTS = inherited

# Keep only the segments worth analysing. The feeder subscribes to far more than the
# strategy should trade -- 55 equities ride along for storage, and analysing them
# costs extra query chunks and ~3x the memory for instruments slope_angle was never
# calibrated on.
# Use ANALYZE_SEGMENTS=ALL to disable the filter. An EMPTY value will not work:
# _env() treats empty as unset and hands back the default.
_seg_raw = _env('ANALYZE_SEGMENTS', 'NSE_FO,BSE_FO,MCX_FO')
ANALYZE_SEGMENTS = ([] if _seg_raw.strip().upper() in ('ALL', '*')
                    else [x.strip().upper() for x in _seg_raw.split(',') if x.strip()])
if ANALYZE_SEGMENTS:
    ANALYZE_INSTRUMENTS = [k for k in ANALYZE_INSTRUMENTS
                           if k.split('|', 1)[0].upper() in ANALYZE_SEGMENTS]
NSE_JSON_PATH = Path(_env(
    'NSE_JSON_PATH',
    str(REPO_ROOT.parent / 'viz_hedge' / 'data' / 'NSE.json'),
))

# ── Engine loop ───────────────────────────────────────────────────────────────
POLL_INTERVAL_SECS = float(_env('POLL_INTERVAL_SECS', '5'))
# Must hold ANGLE_MIN_SAMPLES + ANGLE_N2 ticks even for the SLOWEST instrument.
# At MCX rates (~0.09 ticks/s) 60 minutes is only ~320 ticks; 180 gives ~970.
LOOKBACK_MINUTES   = int(_env('LOOKBACK_MINUTES', '180'))
ENGINE_START       = _env('ENGINE_START', '09:16')   # IST HH:MM
ENGINE_STOP        = _env('ENGINE_STOP', '23:30')    # IST HH:MM — MCX runs late
# Per-exchange close. After its close an instrument is skipped: it cannot tick, so
# querying it wastes rows and its stale view would keep re-triggering the strategy.
EXCHANGE_CLOSE = {}
for _p in _env('EXCHANGE_CLOSE', 'NSE:15:30,BSE:15:30,MCX:23:30').split(','):
    _bits = _p.split(':')
    if len(_bits) == 3:
        EXCHANGE_CLOSE[_bits[0].strip().upper()] = f'{_bits[1]}:{_bits[2]}'

# ── Strategy: slope_angle ─────────────────────────────────────────────────────
ANGLE_N1             = int(_env('ANGLE_N1', '50'))     # middle point, n-N1
ANGLE_N2             = int(_env('ANGLE_N2', '80'))     # oldest point, n-N2
ANGLE_THRESHOLD_DEG  = float(_env('ANGLE_THRESHOLD_DEG', '60'))
ANGLE_PRICE_MODE     = _env('ANGLE_PRICE_MODE', 'pct')  # abs (Rs/tick) | pct (%/tick)

# Threshold: a fixed angle cannot survive a volatility regime change, so by
# default fire on the top (1-q) of the instrument's OWN recent angle distribution.
# Calibrated 2026-07-29 on BSE SENSEX 77500 CE: q=0.95 held up in both halves of
# the day (PF 1.82 / 2.01) where q>=0.99 flipped sign between them.
ANGLE_THRESH_MODE    = _env('ANGLE_THRESH_MODE', 'percentile')  # fixed|percentile|mad
ANGLE_WINDOW         = int(_env('ANGLE_WINDOW', '2000'))   # CAP on the adaptive window
# Minimum angle samples before a threshold is trusted. This, not ANGLE_WINDOW, sets
# the warm-up. MCX options tick ~0.09/s vs NIFTY's 1.5/s, so requiring half of a
# 2,000 window made them unreachable: LOOKBACK_MINUTES never held that many ticks.
ANGLE_MIN_SAMPLES    = int(_env('ANGLE_MIN_SAMPLES', '200'))
ANGLE_Q              = float(_env('ANGLE_Q', '0.95'))      # percentile mode
ANGLE_MAD_K          = float(_env('ANGLE_MAD_K', '5'))     # mad mode

# Long-only: buy CE on an upward bend. Downside is captured by running the same
# signal on the PE, not by shorting.
ANGLE_LONG_ONLY      = _env('ANGLE_LONG_ONLY', 'true').lower() == 'true'
ANGLE_REQUIRE_CONVEX = _env('ANGLE_REQUIRE_CONVEX', 'true').lower() == 'true'
ANGLE_EXIT_ON_REVERSE = _env('ANGLE_EXIT_ON_REVERSE', 'true').lower() == 'true'

# ── Exits (live) ─────────────────────────────────────────────────────────────
# A FIXED % stop cannot work across instruments or regimes. Measured: a 1.5% stop
# was inside a single tick of noise on SENSEX expiry day (tick p95 = 1.48%) and got
# stopped out one second before a 108% move; the same 1.5% was fine the day before.
# Prefer the sigma multiples: stop = K x sigma x sqrt(horizon), computed per
# instrument at entry. The fixed values are the fallback when sigma is unavailable.
EXIT_STOP_SIGMA      = float(_env('EXIT_STOP_SIGMA', '1.0'))    # 0 = use EXIT_STOP_PCT
EXIT_TRAIL_SIGMA     = float(_env('EXIT_TRAIL_SIGMA', '2.0'))   # 0 = use EXIT_TRAIL_PCT
EXIT_SIGMA_WINDOW    = int(_env('EXIT_SIGMA_WINDOW', '200'))    # ticks
EXIT_SIGMA_HORIZON   = int(_env('EXIT_SIGMA_HORIZON', '50'))    # ticks
EXIT_STOP_PCT        = float(_env('EXIT_STOP_PCT', '3.0'))      # fallback
EXIT_TRAIL_PCT       = float(_env('EXIT_TRAIL_PCT', '6.0'))     # fallback
EXIT_TRAIL_AFTER_PCT = float(_env('EXIT_TRAIL_AFTER_PCT', '0')) # 0 = same as trail
EXIT_TARGET_PCT      = float(_env('EXIT_TARGET_PCT', '0'))      # 0 = no cap, let it run
EXIT_MAX_HOLD_SECS   = float(_env('EXIT_MAX_HOLD_SECS', '900'))

# ── Execution ─────────────────────────────────────────────────────────────────
ORDER_MODE        = _env('ORDER_MODE', 'paper')      # signals_only | paper | live
ORDER_QTY_DEFAULT = int(_env('ORDER_QTY_DEFAULT', '1'))

# Size in LOTS per underlying; the actual quantity is lots x lot_size, and lot_size
# is read from the instrument master (NIFTY 65, BANKNIFTY 30, SENSEX from BSE.json)
# rather than hardcoded -- exchanges revise lot sizes and a stale constant would
# silently misprice every trade.
#   LOTS_BY_UNDERLYING=NIFTY:5,SENSEX:10,CRUDEOILM:1
LOTS_BY_UNDERLYING = {}
for _pair in _env('LOTS_BY_UNDERLYING',
                  'NIFTY:5,SENSEX:10,CRUDEOILM:1,NATURALGAS:1,SILVERM:1').split(','):
    if ':' in _pair:
        _u, _n = _pair.split(':', 1)
        try:
            LOTS_BY_UNDERLYING[_u.strip().upper()] = int(_n)
        except ValueError:
            pass
SLIPPAGE_BPS      = float(_env('SLIPPAGE_BPS', '5'))

UPSTOX_ACCESS_TOKEN  = _env('UPSTOX_ACCESS_TOKEN')
UPSTOX_ORDER_SANDBOX = _env('UPSTOX_ORDER_SANDBOX', 'false').lower() == 'true'

# ── Risk gate ─────────────────────────────────────────────────────────────────
SIGNAL_COOLDOWN_SECS = int(_env('SIGNAL_COOLDOWN_SECS', '300'))
MAX_OPEN_POSITIONS   = int(_env('MAX_OPEN_POSITIONS', '5'))
MAX_ORDER_NOTIONAL   = float(_env('MAX_ORDER_NOTIONAL', '500000'))
DAILY_LOSS_LIMIT     = float(_env('DAILY_LOSS_LIMIT', '10000'))
KILL_SWITCH_FILE     = Path(_env('KILL_SWITCH_FILE', str(REPO_ROOT / 'KILL_SWITCH')))

# ── Alerts ───────────────────────────────────────────────────────────────────
# log | ntfy | telegram | whatsapp | twilio | off
NOTIFY_BACKEND         = _env('NOTIFY_BACKEND', 'log')
# "Strong" = cleared its own adaptive bar by this much. On the 30 Jul SENSEX move
# the real breakout ran angle/threshold = 1.43 while marginal triggers sat at 1.07.
# Calibrated on two real days of BSE SENSEX 77500 CE rather than guessed.
# ratio 1.3 + a 15-minute cooldown gives ~11 alerts on a wild expiry day and ~14 on
# a normal one, PER INSTRUMENT, and still catches the +122% move. Raising the ratio
# to 1.8 cuts to 3-4 alerts but drops the biggest move (133% -> 63%): too selective.
NOTIFY_MIN_ANGLE_RATIO = float(_env('NOTIFY_MIN_ANGLE_RATIO', '1.3'))
# The recent-leg move does NOT transfer between instruments or regimes: median at a
# trigger was 8.56% on expiry day. Left at 0 (off) rather than shipping a number
# that is a no-op on one day and mutes everything on another.
NOTIFY_MIN_MOVE_PCT    = float(_env('NOTIFY_MIN_MOVE_PCT', '0'))
NOTIFY_COOLDOWN_SECS   = int(_env('NOTIFY_COOLDOWN_SECS', '900'))
# Hard stop on volume: alerts cost money on WhatsApp and attention everywhere.
NOTIFY_MAX_PER_DAY     = int(_env('NOTIFY_MAX_PER_DAY', '40'))
NOTIFY_ACTIONS         = [a.strip().upper() for a in
                          _env('NOTIFY_ACTIONS', 'ENTER_LONG').split(',') if a.strip()]

NTFY_URL        = _env('NTFY_URL', 'https://ntfy.sh')
NTFY_TOPIC      = _env('NTFY_TOPIC', '')
TELEGRAM_TOKEN  = _env('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = _env('TELEGRAM_CHAT_ID', '')
# WhatsApp Cloud API — business-initiated sends REQUIRE an approved template
WHATSAPP_TOKEN    = _env('WHATSAPP_TOKEN', '')
WHATSAPP_PHONE_ID = _env('WHATSAPP_PHONE_ID', '')
WHATSAPP_TO       = _env('WHATSAPP_TO', '')          # E.164, e.g. 9198XXXXXXXX
WHATSAPP_TEMPLATE = _env('WHATSAPP_TEMPLATE', 'viz_signal_alert')
WHATSAPP_LANG     = _env('WHATSAPP_LANG', 'en')
TWILIO_SID   = _env('TWILIO_SID', '')
TWILIO_TOKEN = _env('TWILIO_TOKEN', '')
TWILIO_FROM  = _env('TWILIO_FROM', 'whatsapp:+14155238886')
TWILIO_TO    = _env('TWILIO_TO', '')

# ── Persistence ───────────────────────────────────────────────────────────────
PERSIST_TO_INFLUX = _env('PERSIST_TO_INFLUX', 'true').lower() == 'true'
JOURNAL_DIR       = Path(_env('JOURNAL_DIR', str(REPO_ROOT / 'journal')))


def validate() -> list[str]:
    """Return a list of fatal config errors (empty = OK)."""
    errors = []
    if not INFLUX_TOKEN or not INFLUX_ORG:
        errors.append('INFLUX_TOKEN and INFLUX_ORG must be set')
    if not ANALYZE_INSTRUMENTS:
        errors.append('ANALYZE_INSTRUMENTS is empty — nothing to analyze')
    if ORDER_MODE not in ('signals_only', 'paper', 'live'):
        errors.append(f'ORDER_MODE must be signals_only|paper|live (got {ORDER_MODE!r})')
    if ORDER_MODE == 'live' and not UPSTOX_ACCESS_TOKEN:
        errors.append('ORDER_MODE=live requires UPSTOX_ACCESS_TOKEN')
    if ANGLE_N2 <= ANGLE_N1 or ANGLE_N1 <= 0:
        errors.append(f'require 0 < ANGLE_N1 < ANGLE_N2 (got {ANGLE_N1}, {ANGLE_N2})')
    if ANGLE_PRICE_MODE not in ('abs', 'pct'):
        errors.append(f"ANGLE_PRICE_MODE must be abs|pct (got {ANGLE_PRICE_MODE!r})")
    if ANGLE_THRESH_MODE not in ('fixed', 'percentile', 'mad'):
        errors.append(f'ANGLE_THRESH_MODE must be fixed|percentile|mad (got {ANGLE_THRESH_MODE!r})')
    if not 0.0 < ANGLE_Q < 1.0:
        errors.append(f'ANGLE_Q must be strictly between 0 and 1 (got {ANGLE_Q})')
    # the adaptive window must actually fit inside the tick history we hold
    if ANGLE_THRESH_MODE != 'fixed':
        need_ticks = ANGLE_MIN_SAMPLES + ANGLE_N2 + 1
        if LOOKBACK_MINUTES * 60 < need_ticks:      # ~1 tick/sec worst case
            errors.append(
                f'LOOKBACK_MINUTES={LOOKBACK_MINUTES} may not hold the '
                f'{need_ticks} ticks the adaptive window needs; raise it or lower ANGLE_WINDOW')
    return errors
