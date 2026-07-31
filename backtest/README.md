# Backtesting and signal analysis

Two different questions, two different tools. Keep them straight:

| question | tool | source |
|---|---|---|
| *Would this have worked?* | `backtest/backtest.py` | tick data (CSV or InfluxDB) |
| *What did it actually do?* | `utils/check_signals.py`, and the **Signals** section of `viz_hedge/notebooks/market_analysis.ipynb` | the `signals` bucket + JSONL journal |

The backtester imports `src/strategies/angle_math.py` — the exact module the live
engine uses — so a backtested edge and a live signal can never disagree about what
an angle is. If they diverge, the bug is in data plumbing, not the maths.

---

## 1. Get the data

Straight from InfluxDB (no export step):

```bash
python backtest/backtest.py --influx \
    --measurement "BSE_SENSEX 77500 CE 06 AUG 26" --date 2026-07-31 --sweep
```

Or export a CSV once and iterate fast on it — the notebook's *Save ticks to CSV*
cell writes the `time,ltp` format this expects. CSV is much quicker when sweeping,
since each sweep runs dozens of simulations.

Measurement names come from the feeder: `{EXCH}_{trading_symbol}`. List them with
the *feeder is streaming* cell in the notebook, or `check_feed.py`.

---

## 2. Calibrate, in this order

Each step assumes the previous one is fixed. **Re-sweep after any structural
change** — when we switched from long+short to long-only, the best threshold moved
from the top 1% of angles to the top 5%.

```bash
# a. threshold: how selective should it be?
python backtest/backtest.py --csv ticks.csv --sweep

# b. geometry: is n1=50 / n2=80 the right lookback?
python backtest/backtest.py --csv ticks.csv --sweep --grid

# c. exits: fixed stop/target ...
python backtest/backtest.py --csv ticks.csv --sweep-exits

#    ... or volatility-scaled (stop = K x sigma x sqrt(horizon))
python backtest/backtest.py --csv ticks.csv --sweep-sigma
```

Read the grids for **broad plateaus, not peaks**. A single bright cell surrounded
by bad ones is noise; a block of adjacent cells that all work is structure. The
best-looking cell we ever found (+82.7, PF 1.85) was negative in the first half of
the same day.

---

## 3. Validate before you believe it

```bash
python backtest/backtest.py --csv ticks.csv --price-mode pct \
    --thresh-mode percentile --q 0.95 --window 2000 \
    --stop-pct 1.5 --target-pct 3 --max-hold 900 --cooldown 100 --validate
```

Three checks, in increasing order of how often they have caught us out:

1. **direction split** — profit concentrated on one side may just be the day's drift
2. **split-half** — same parameters on each half of the session. A sign flip between
   halves means the full-day number is an artifact. This is the cheapest
   out-of-sample test available and it has vetoed more configurations than anything else.
3. **null test** — random entries with the *same* exit rules. Exit geometry alone
   generates P&L, so the bar is beating random, not beating zero. `p < 0.05` to pass.

A healthy result looks like: both halves same sign, and the strategy beating ~100%
of random runs *whose mean is negative* — that last part is what proves it is
selecting moves rather than riding drift.

---

## 4. Look at it

```bash
python backtest/plot_triggers.py --csv ticks.csv --price-mode pct \
    --thresh-mode percentile --q 0.95 -o triggers.html
```

Three panels: LTP with entries/exits, the angle against the moving threshold, and
cumulative P&L. The gap between *raw triggers* and *trades* is usually the most
informative thing on the chart — 260 triggers became 32 trades in one run, the rest
suppressed by the cooldown and the open-position check.

---

## 5. Compare live against backtest

After a session, run the backtester over the same day and instrument with the
parameters the engine was running, then compare with `utils/check_signals.py --tail`
and the notebook's Signals section. Entry times should line up closely. They will not
match exactly — the live engine polls every `POLL_INTERVAL_SECS` and only sees ticks
already written to InfluxDB, so it fires on the first poll *after* the crossing,
whereas the backtest fires on the exact tick. Systematic divergence beyond that
(different instruments, wildly different counts) means a config or data mismatch,
not strategy behaviour.

---

## Flag reference

| flag | meaning |
|---|---|
| `--n1 / --n2` | the `n-50` / `n-80` sample offsets, in ticks |
| `--price-mode` | `pct` = %/tick (portable) · `abs` = ₹/tick (per-instrument threshold) |
| `--thresh-mode` | `percentile` (adaptive, default) · `mad` · `fixed` |
| `--q` | percentile quantile; 0.95 = top 5% of recent angles |
| `--window` | ticks in the adaptive window |
| `--stop-pct / --target-pct` | fixed exits, % of entry |
| `--trail-pct / --trail-after-pct` | trailing stop, and the profit at which it arms |
| `--stop-sigma / --trail-sigma` | volatility-scaled exits (`K x sigma x sqrt(horizon)`) |
| `--max-hold / --cooldown` | tick cap on a trade; ticks to wait after an exit |
| `--allow-short` | off by default — downside is traded via the PE |
| `--no-convex` | drop the `slope_full > slope_base` acceleration filter |
| `--validate` | direction split + split-half + null test |
| `--save-trades FILE` | dump the trade list as CSV |

Fixed thresholds do not transfer: on BSE SENSEX 77500 CE the maximum angle observed
was 20° in `pct` mode and 48° in `abs`, so the original 60° never fires in either.
