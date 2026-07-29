"""All configuration, env-driven. See docs §11 for the reference table."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── InfluxDB (same instance viz_hedge writes to) ──────────────────────────────
INFLUX_URL   = os.getenv('INFLUX_URL', 'http://localhost:8086')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN')
INFLUX_ORG   = os.getenv('INFLUX_ORG')
# viz_hedge writes every day into ONE bucket with dateless measurements
# ({EXCH}_{trading_symbol}); the timestamp separates days. Per-day buckets are
# retired -- they hit InfluxDB Cloud's bucket-count quota.
INFLUX_BUCKET  = os.getenv('INFLUX_BUCKET', 'tick_data')
SIGNALS_BUCKET = os.getenv('SIGNALS_BUCKET', 'signals')

# Query shaping. A single pivoted query over every instrument x a long window is
# enough to blow the HTTP read timeout: 6 instruments x one session was ~186k rows
# and timed out at 60s, so a cold start over 79 instruments x LOOKBACK_MINUTES
# (~370k rows) certainly would. Chunk the instruments, and pull only the fields the
# strategy actually reads -- InfluxDB bills per byte scanned as well as per query.
INFLUX_QUERY_CHUNK = int(os.getenv('INFLUX_QUERY_CHUNK', '20'))   # instruments/query
INFLUX_QUERY_TIMEOUT_MS = int(os.getenv('INFLUX_QUERY_TIMEOUT_MS', '120000'))
# Chunks run concurrently: EC2 is ap-south-1 and InfluxDB Cloud us-east-1, so each
# query pays ~4s of transcontinental round trip regardless of how little it returns.
INFLUX_QUERY_WORKERS = int(os.getenv('INFLUX_QUERY_WORKERS', '4'))
INFLUX_FIELDS = [f.strip() for f in os.getenv('INFLUX_FIELDS', 'ltp,vtt,oi').split(',')
                 if f.strip()]                                    # empty = every field

# ── Instruments ───────────────────────────────────────────────────────────────
ANALYZE_INSTRUMENTS = [
    k.strip() for k in os.getenv('ANALYZE_INSTRUMENTS', '').split(',') if k.strip()
]
NSE_JSON_PATH = Path(os.getenv(
    'NSE_JSON_PATH',
    str(REPO_ROOT.parent / 'viz_hedge' / 'data' / 'NSE.json'),
))

# ── Engine loop ───────────────────────────────────────────────────────────────
POLL_INTERVAL_SECS = float(os.getenv('POLL_INTERVAL_SECS', '5'))
LOOKBACK_MINUTES   = int(os.getenv('LOOKBACK_MINUTES', '60'))
ENGINE_START       = os.getenv('ENGINE_START', '09:16')   # IST HH:MM
ENGINE_STOP        = os.getenv('ENGINE_STOP', '15:30')    # IST HH:MM

# ── Strategy: slope_angle ─────────────────────────────────────────────────────
ANGLE_N1             = int(os.getenv('ANGLE_N1', '50'))     # middle point, n-N1
ANGLE_N2             = int(os.getenv('ANGLE_N2', '80'))     # oldest point, n-N2
ANGLE_THRESHOLD_DEG  = float(os.getenv('ANGLE_THRESHOLD_DEG', '60'))
ANGLE_PRICE_MODE     = os.getenv('ANGLE_PRICE_MODE', 'pct')  # abs (Rs/tick) | pct (%/tick)

# Threshold: a fixed angle cannot survive a volatility regime change, so by
# default fire on the top (1-q) of the instrument's OWN recent angle distribution.
# Calibrated 2026-07-29 on BSE SENSEX 77500 CE: q=0.95 held up in both halves of
# the day (PF 1.82 / 2.01) where q>=0.99 flipped sign between them.
ANGLE_THRESH_MODE    = os.getenv('ANGLE_THRESH_MODE', 'percentile')  # fixed|percentile|mad
ANGLE_WINDOW         = int(os.getenv('ANGLE_WINDOW', '2000'))   # ticks in adaptive window
ANGLE_Q              = float(os.getenv('ANGLE_Q', '0.95'))      # percentile mode
ANGLE_MAD_K          = float(os.getenv('ANGLE_MAD_K', '5'))     # mad mode

# Long-only: buy CE on an upward bend. Downside is captured by running the same
# signal on the PE, not by shorting.
ANGLE_LONG_ONLY      = os.getenv('ANGLE_LONG_ONLY', 'true').lower() == 'true'
ANGLE_REQUIRE_CONVEX = os.getenv('ANGLE_REQUIRE_CONVEX', 'true').lower() == 'true'
ANGLE_EXIT_ON_REVERSE = os.getenv('ANGLE_EXIT_ON_REVERSE', 'true').lower() == 'true'

# ── Execution ─────────────────────────────────────────────────────────────────
ORDER_MODE        = os.getenv('ORDER_MODE', 'paper')      # signals_only | paper | live
ORDER_QTY_DEFAULT = int(os.getenv('ORDER_QTY_DEFAULT', '1'))
SLIPPAGE_BPS      = float(os.getenv('SLIPPAGE_BPS', '5'))

UPSTOX_ACCESS_TOKEN  = os.getenv('UPSTOX_ACCESS_TOKEN')
UPSTOX_ORDER_SANDBOX = os.getenv('UPSTOX_ORDER_SANDBOX', 'false').lower() == 'true'

# ── Risk gate ─────────────────────────────────────────────────────────────────
SIGNAL_COOLDOWN_SECS = int(os.getenv('SIGNAL_COOLDOWN_SECS', '300'))
MAX_OPEN_POSITIONS   = int(os.getenv('MAX_OPEN_POSITIONS', '5'))
MAX_ORDER_NOTIONAL   = float(os.getenv('MAX_ORDER_NOTIONAL', '500000'))
DAILY_LOSS_LIMIT     = float(os.getenv('DAILY_LOSS_LIMIT', '10000'))
KILL_SWITCH_FILE     = Path(os.getenv('KILL_SWITCH_FILE', str(REPO_ROOT / 'KILL_SWITCH')))

# ── Persistence ───────────────────────────────────────────────────────────────
PERSIST_TO_INFLUX = os.getenv('PERSIST_TO_INFLUX', 'true').lower() == 'true'
JOURNAL_DIR       = Path(os.getenv('JOURNAL_DIR', str(REPO_ROOT / 'journal')))


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
        need_ticks = ANGLE_WINDOW + ANGLE_N2 + 1
        if LOOKBACK_MINUTES * 60 < need_ticks:      # ~1 tick/sec worst case
            errors.append(
                f'LOOKBACK_MINUTES={LOOKBACK_MINUTES} may not hold the '
                f'{need_ticks} ticks the adaptive window needs; raise it or lower ANGLE_WINDOW')
    return errors
