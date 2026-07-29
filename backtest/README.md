# slope_angle — backtesting

The backtester imports `src/strategies/angle_math.py`, the exact module the live
engine uses. Backtest and live can therefore never disagree about what an angle is.

## Quick start

```bash
python backtest/backtest.py --csv ticks.csv --sweep      # find a live threshold
python backtest/backtest.py --csv ticks.csv --threshold 27 --stop-pct 1 --target-pct 2
python backtest/backtest.py --influx --measurement "NSE_HDFCBANK" --date 2026-07-27 --sweep
```

CSV needs two columns: `time`, `ltp` (Section 11 of the Upstox toolbox notebook
exports exactly this).

## Flags that matter

| flag | meaning |
|---|---|
| `--n1 / --n2` | the `n-50` / `n-80` sample offsets, in ticks |
| `--price-mode` | `abs` = ₹/tick (per-instrument threshold) · `pct` = %/tick (portable) |
| `--threshold` | angle in degrees that fires a trigger |
| `--stop-pct / --target-pct` | exits as % of entry |
| `--max-hold` | force-exit after N ticks |
| `--cooldown` | ticks to wait after an exit before re-entering |
| `--flip` | reverse straight into the opposite side instead of going flat |
| `--sweep --grid` | scan thresholds, and optionally n1/n2 geometries |

## Reading the output

`window_s` is the wall-clock span of the `n2`-tick lookback. If the report warns
that entries used windows >120s, those angles straddle a feeder data gap and are
not measuring recent price geometry — fix the feed before trusting the result.
