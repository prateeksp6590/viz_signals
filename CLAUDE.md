# viz_signals — Claude Context

## Project Identity

This repository is the **Signal Engine** (Trading Engine v1) of **Shaaru Aureus Fintech** —
market data analyzer, signal generator, order executor, and P&L tracker.

**Firm:** Shaaru Aureus Fintech
**Owner:** Prateek Vishwakarma (CEO / CTO)
**Sibling repo:** `viz_hedge` (market data feeder — writes the InfluxDB tick data this app reads)

## What This Repo Does

Every `POLL_INTERVAL_SECS` during market hours (09:16–15:30 IST):

1. Pulls fresh ticks per instrument from InfluxDB `tick_data_YYYYMMDD` (written by viz_hedge)
2. Maintains rolling views: raw ticks, OHLCV bars (any interval), greeks/IV series
3. Runs the pluggable **Strategy** → `Signal` objects per instrument
4. Filters signals (cooldowns, position state) → pre-trade **RiskGate** (limits, kill switch)
5. Executes via broker: `paper` (simulated fills, default) / `live` (Upstox v3 API) / `signals_only`
6. Tracks positions: MTM unrealized P&L, realized on exit, max favorable/adverse excursion
7. Journals everything: JSONL under `journal/YYYYMMDD/` + InfluxDB `signals_YYYYMMDD` bucket

**Full design:** `docs/shaaru-aureus-signal-engine.md` — read before significant changes.
Platform-wide docs live in `../viz_hedge/docs/`.

## The Algorithm Drop-In Point

The proprietary algorithm subclasses `Strategy` (`src/strategies/base.py`) and implements
`generate_signals(view) -> list[Signal]`. Wire it via the `STRATEGY` constant at the top of
`src/main.py`. `ExampleSmaCross` is a placeholder only.

`view` (InstrumentView) exposes: `.ltp`, `.ticks` (DataFrame of ltp/ltq/vtt/oi/tbq/tsq/greeks),
`.bars('1min')` / `.bars('5min')` (OHLCV), `.greeks`, `.position` (open Position or None).

## Codebase Structure

```
src/
  main.py                     — market-hours gate, poll loop, EOD flatten, STRATEGY wiring
  models.py                   — Signal, Order, Fill, Position + enums
  config/settings.py          — all env config + validate()
  services/
    influx_reader.py          — Flux queries; measurement naming mirrors viz_hedge
    market_view.py            — InstrumentView + MarketData (incremental refresh)
    signal_engine.py          — strategy runner + eligibility (cooldown, position state)
    risk_gate.py              — kill switch, position/notional/daily-loss limits
    position_tracker.py       — fills → positions, MTM, realized P&L, MFE/MAE
    journal.py                — JSONL + signals_YYYYMMDD InfluxDB persistence
    brokers/{base,paper,upstox_live}.py
  strategies/{base,example_sma_cross}.py
```

## Key Conventions

- Reads viz_hedge's schema: bucket `tick_data_YYYYMMDD`, measurement `NSE_{symbol}_{YYYYMMDD}`
  (fallback `NSE_FO_{token}_…`); symbol resolution via `NSE.json` at `NSE_JSON_PATH`
  (default `../viz_hedge/data/NSE.json`)
- One open position per instrument; entries blocked on `NSE_INDEX|*` keys (signal-only)
- Kill switch: create file `KILL_SWITCH` in repo root → entries halt within one cycle; exits still allowed
- `ORDER_MODE=live` requires daily `UPSTOX_ACCESS_TOKEN` (viz_hedge `utils/generate_token.py`);
  `UPSTOX_ORDER_SANDBOX=true` routes to api-sandbox.upstox.com
- All timestamps UTC internally; IST only at the market-hours gate

## Environment Variables

See `.env.example` (complete) and `docs/shaaru-aureus-signal-engine.md` §11 (reference table).
Required: `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `ANALYZE_INSTRUMENTS`.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in Influx credentials + instruments
python run.py          # waits for 09:16 IST, stops 15:30, flattens paper positions
```

viz_hedge must be running (or have run today) to populate `tick_data_YYYYMMDD`.

## Deployment & Front-End (designed, not yet built)

- **Cloud** (docs §13): co-located on the feeder's VM (EC2 t3.small, Mumbai, IST clock) as a
  `vizsignals-engine` systemd unit — timer 09:10 Mon–Fri, app self-gates 09:16–15:30, exit 0.
  Reuses the feeder's daily token automation and NSE.json. See also
  `../viz_hedge/docs/shaaru-aureus-cloud-deployment-runbook.md`.
- **InfluxDB is Cloud-hosted** (existing account), not on the VM — the VM is stateless.
  Watch per-cycle query latency (one Flux query per instrument per poll) and note the daily
  bucket pattern needs the usage-based plan (free tier caps at 2 buckets).
- **Front-end** (docs §14): responsive web app (PWA), not native mobile. Phase A: Grafana Cloud
  reading InfluxDB Cloud directly (`signals_YYYYMMDD`). Phase B: FastAPI + React dashboard
  reading persisted state only (InfluxDB + journal) — never the engine process; sole write
  path is an auth-gated kill-switch button.

## Known Gaps (v1)

See docs §15: cloud provisioning, front-end phases, live fill stream (portfolio WebSocket),
restart recovery from journal, Kafka topics, multi-strategy weighting, position sizing,
NSE holiday calendar, containerization.
