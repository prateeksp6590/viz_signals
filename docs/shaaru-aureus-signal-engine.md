# Shaaru Aureus Fintech — Signal Engine (viz_signals)

**Component:** Trading Engine v1 — Market Data Analyzer, Signal Generator & Order Executor
**Repo:** `viz_signals` (sibling of `viz_hedge`)
**Owner:** Prateek Vishwakarma (CEO / CTO)
**Status:** Scaffold — awaiting proprietary algorithm drop-in
**Last updated:** 2026-07-03

---

## 1. What This Component Does

viz_signals is the first implementation slice of the **Trading Engine** described in
`shaaru-aureus-architecture.md` §5.2, adapted to the current platform reality:

- Market data comes from **InfluxDB** (written by viz_hedge), not Kafka — Kafka is a later phase.
- The broker is **Upstox** (Indian markets), not IBKR/Alpaca.
- Execution starts in **paper mode**; live order placement exists behind a flag.

Each poll cycle it:

1. Pulls fresh ticks for every configured instrument from today's `tick_data_YYYYMMDD` bucket
2. Maintains a rolling in-memory view per instrument — raw ticks, OHLCV bars, greeks/IV series
3. Runs the pluggable **Strategy** (the proprietary algorithm) against each view
4. Filters emitted signals for eligibility (cooldowns, position state)
5. Passes eligible signals through the **pre-trade risk gate**
6. Executes approved orders — simulated fills (paper) or Upstox API (live)
7. Marks all open positions to market — unrealized P&L, max favorable/adverse excursion
8. Journals every signal, order, fill, and position snapshot (JSONL + InfluxDB)

---

## 2. Platform Fit

```
Upstox WebSocket (NSE market data)
        ↓
  viz_hedge (feeder)                        ← runs 09:15–15:28 IST
        ↓
  InfluxDB  tick_data_YYYYMMDD
        ↓  poll every POLL_INTERVAL_SECS
┌─────────────────────────────────────────┐
│  viz_signals (this repo)                │  ← runs 09:16–15:30 IST
│                                         │
│  InfluxReader → MarketView (per instr)  │
│        ↓                                │
│  Strategy (pluggable algorithm)         │
│        ↓  Signal[]                      │
│  SignalEngine (eligibility, cooldowns)  │
│        ↓                                │
│  RiskGate (limits, kill switch)         │
│        ↓                                │
│  Broker (paper | upstox_live)           │
│        ↓  Fill                          │
│  PositionTracker (MTM P&L, MFE/MAE)     │
│        ↓                                │
│  Journal (JSONL + signals_YYYYMMDD)     │
└─────────────────────────────────────────┘
        ↓
  Front-end dashboard (§14: Grafana → web app)
        ↓  [future]
  Kafka orders/executions topics
```

The architecture doc's flow — *Signal Generator → Pre-trade Risk Check → Order Generator →
Order Router*, with an *Execution Monitor* feeding back — maps 1:1 onto
`signal_engine → risk_gate → brokers → position_tracker`.

---

## 3. Scope

### In scope (v1)

| Capability | Notes |
|---|---|
| InfluxDB tick reader | Incremental fetch per instrument, rolling lookback window |
| Bars / ticks / greeks views | 1-min & 5-min OHLCV (any pandas interval), raw ticks, IV/greeks series |
| Pluggable strategy interface | One class to implement; algorithm supplied by owner |
| Signal eligibility | Per-instrument cooldown, no duplicate entries, exit-only-if-open |
| Pre-trade risk gate | Max open positions, per-order notional cap, daily loss halt, kill-switch file, index-instrument block |
| Paper execution | Immediate fills at LTP ± slippage bps |
| Live execution (flagged) | Upstox v3 Place Order API; sandbox endpoint supported |
| Position & P&L tracking | Avg entry, MTM unrealized, realized on exit, MFE/MAE price excursions |
| EOD auto-flatten (paper) | All paper positions closed at engine stop time |
| Journaling | Append-only JSONL per day + InfluxDB `signals_YYYYMMDD` bucket |

### Designed, delivered in phases

- **Cloud deployment** — runs unattended on the platform VM per market hours (§13)
- **Front-end** — monitoring dashboard for signals, positions, P&L, orders (§14)

### Out of scope (v1 — future phases)

- Kafka consumption/publication (`market-data`, `orders`, `executions` topics)
- Multi-strategy aggregation & weighting (single strategy per process in v1)
- Live fill tracking via Upstox portfolio WebSocket (v1 polls order details)
- Multi-leg options orders, order slicing, smart routing
- Restart recovery of open live positions from broker (paper positions recoverable from journal — manual)
- Position sizing models (fixed `ORDER_QTY_DEFAULT` in v1; strategy may override per signal)
- Native iOS/Android app (responsive web dashboard first — §14)
- Datadog metrics / health endpoint

---

## 4. Module Layout

```
viz_signals/
  run.py                        — entry point
  requirements.txt
  .env.example
  CLAUDE.md
  docs/shaaru-aureus-signal-engine.md   — this document
  journal/YYYYMMDD/             — daily JSONL journals (gitignored)
  src/
    main.py                     — IST market-hours gate, poll loop, graceful shutdown
    models.py                   — Signal, Order, Fill, Position dataclasses + enums
    config/settings.py          — all env-driven configuration
    services/
      influx_reader.py          — Flux queries against tick_data_YYYYMMDD
      market_view.py            — rolling per-instrument view: ticks / bars / greeks
      signal_engine.py          — runs strategy, applies eligibility rules
      risk_gate.py              — pre-trade checks + kill switch
      position_tracker.py       — open positions, MTM, realized P&L, MFE/MAE
      journal.py                — JSONL + InfluxDB persistence
      brokers/
        base.py                 — Broker abstract interface
        paper.py                — simulated fills
        upstox_live.py          — Upstox v3 order placement (ORDER_MODE=live)
    strategies/
      base.py                   — Strategy ABC (the drop-in point)
      example_sma_cross.py      — placeholder SMA-crossover example
    utils/logger.py             — structured logger (mirrors viz_hedge)
```

---

## 5. Data Contracts

### Signal

Emitted by the strategy. One signal = one intent on one instrument.

| Field | Type | Notes |
|---|---|---|
| `instrument_key` | str | Upstox key, e.g. `NSE_FO\|72272` |
| `symbol` | str | Trading symbol, e.g. `NIFTY26JUL24200CE` |
| `action` | enum | `ENTER_LONG` / `ENTER_SHORT` / `EXIT` |
| `price` | float | LTP at signal time (reference, not a limit) |
| `ts` | datetime | Signal generation time (UTC) |
| `strategy` | str | Strategy name for attribution |
| `confidence` | float | 0–1; risk gate may use later for sizing |
| `qty` | int \| None | Strategy override; `None` → `ORDER_QTY_DEFAULT` |
| `reason` | str | Human-readable explanation (journaled) |

### Order / Fill

Orders carry `mode` (`paper`/`live`), `status` (`FILLED`/`PENDING`/`REJECTED`), and a
reference to the originating signal. Fills carry executed price/qty/timestamp. In paper
mode every approved order fills immediately at `ltp ± SLIPPAGE_BPS`. In live mode the
Upstox order id is stored and order details are polled briefly for the average fill price.

### Position

One open position per instrument (v1 — no pyramiding).

| Field | Notes |
|---|---|
| `side` | LONG / SHORT |
| `qty`, `avg_entry`, `entry_ts` | From the entry fill |
| `last_price`, `unrealized_pnl` | Updated every poll cycle |
| `max_favorable`, `max_adverse` | Best/worst price excursion since entry (price movement tracking) |
| `realized_pnl`, `exit_price`, `exit_ts` | Set on close |

P&L convention: `unrealized = (last − avg_entry) × qty × dir` where `dir = +1` long, `−1` short.

---

## 6. Strategy Interface (the algorithm drop-in point)

The proprietary algorithm implements one class:

```python
from src.strategies.base import Strategy
from src.models import Signal, SignalAction

class MyAlgorithm(Strategy):
    name = "my_algorithm_v1"

    def generate_signals(self, view) -> list[Signal]:
        # view.instrument_key   → 'NSE_FO|72272'
        # view.symbol           → 'NIFTY26JUL24200CE'
        # view.ltp              → latest traded price (float | None)
        # view.ticks            → DataFrame: ltp, ltq, vtt, oi, tbq, tsq,
        #                          iv, delta, theta, gamma, vega, rho (UTC index)
        # view.bars('1min')     → DataFrame: open, high, low, close, volume
        # view.bars('5min')     → any pandas offset alias
        # view.greeks           → DataFrame: iv, delta, theta, gamma, vega, rho
        # view.position         → open Position on this instrument, or None
        ...
        return [Signal(..., action=SignalAction.ENTER_LONG, ...)]
```

Wire it in `src/main.py` (one line — replace `ExampleSmaCross()` with `MyAlgorithm()`).
The engine calls `generate_signals` once per instrument per poll cycle. Strategies are
stateless-by-default but may keep internal state; `view.position` exposes current
position state so the algorithm can emit exits.

---

## 7. Market Data Access

- **Source:** today's `tick_data_YYYYMMDD` bucket; measurement `NSE_{symbol}_{date}`
  resolved through the same instrument-key → trading-symbol map viz_hedge uses
  (reads `NSE.json` at `NSE_JSON_PATH`, default `../viz_hedge/data/NSE.json`).
- **Incremental fetch:** first query pulls `LOOKBACK_MINUTES` of history; subsequent
  queries pull only rows newer than the last seen timestamp. Views trim to the lookback
  window each cycle.
- **Bars:** pandas resample of tick LTP → OHLC. Bar volume = diff of cumulative `vtt`
  resampled last-per-bar (approximate at bar boundaries — acceptable for v1; exact
  per-trade aggregation is a later refinement). Index instruments have no volume/OI —
  those columns are absent and volume is 0.
- **Greeks:** subset of tick fields (`iv, delta, theta, gamma, vega, rho`), rows where
  at least one is present.

---

## 8. Eligibility & Risk Gate

Signal eligibility (in `signal_engine`):

1. `ENTER_*` suppressed if a position is already open on the instrument
2. `EXIT` suppressed if no position is open
3. Per-(instrument, action) cooldown: `SIGNAL_COOLDOWN_SECS` between identical signals

Pre-trade risk gate (in `risk_gate`), applied to entries:

| Check | Config | Behavior on breach |
|---|---|---|
| Kill switch | `KILL_SWITCH_FILE` exists | Reject all **entries**; exits still allowed |
| Max open positions | `MAX_OPEN_POSITIONS` | Reject entry |
| Per-order notional | `MAX_ORDER_NOTIONAL` (qty × price) | Reject entry |
| Daily loss halt | realized + unrealized ≤ −`DAILY_LOSS_LIMIT` | Reject entries for rest of day |
| Non-tradeable instrument | key starts `NSE_INDEX` | Reject (indices are signal-only) |

Every rejection is journaled with the reason. The kill switch is a plain file — `touch
KILL_SWITCH` in the repo root halts new entries within one poll cycle; delete it to resume.

---

## 9. Execution Modes

`ORDER_MODE` selects the broker:

- **`signals_only`** — signals are generated and journaled; no orders, no positions.
- **`paper`** (default) — immediate simulated fill at `ltp × (1 ± SLIPPAGE_BPS/10000)`.
  Full position/P&L lifecycle. Zero capital risk.
- **`live`** — Upstox v3 Place Order:
  - `POST {base}/v3/order/place` with MARKET/DAY/intraday (`product=I`) parameters,
    `tag=viz_signals` for blotter attribution
  - `base` = `https://api.upstox.com`, or `https://api-sandbox.upstox.com` when
    `UPSTOX_ORDER_SANDBOX=true` (Upstox sandbox supports order endpoints only)
  - After placement, order details are polled (up to ~5 s) for the average fill price;
    if still pending, the position is marked provisionally at LTP and the order left
    `PENDING` in the journal — **v1 limitation**, superseded later by the Upstox
    portfolio-stream WebSocket
  - Requires `UPSTOX_ACCESS_TOKEN` (same daily token flow as viz_hedge)

EOD (engine stop, default 15:30 IST): paper positions are auto-flattened at last mark and
realized P&L journaled. Live positions are **not** auto-squared in v1 — rely on the
broker's intraday auto square-off, and a warning is logged for any live position still open.

---

## 10. Persistence

**JSONL journal** (append-only, `journal/YYYYMMDD/`): `signals.jsonl`, `orders.jsonl`,
`fills.jsonl`, `positions.jsonl` (snapshot per position per cycle + terminal close record),
`rejections.jsonl`. This is the audit trail — precursor of the architecture's journaled
order log.

**InfluxDB** (`PERSIST_TO_INFLUX=true`, bucket `signals_YYYYMMDD`, auto-created like the
feeder's): measurements `signal`, `order`, `position` tagged by `symbol` / `strategy` /
`action`-or-`side`, so Grafana or notebooks can chart signals and P&L against the tick data.

---

## 11. Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `INFLUX_URL` | ✅ | `http://localhost:8086` | InfluxDB Cloud endpoint (same account the feeder writes to), e.g. `https://<region>.aws.cloud2.influxdata.com` |
| `INFLUX_TOKEN` / `INFLUX_ORG` | ✅ | — | InfluxDB credentials |
| `ANALYZE_INSTRUMENTS` | ✅ | — | Comma-separated Upstox instrument keys (typically the feeder's `SUBSCRIBE_INSTRUMENTS`) |
| `NSE_JSON_PATH` | ❌ | `../viz_hedge/data/NSE.json` | Instrument master for symbol resolution |
| `POLL_INTERVAL_SECS` | ❌ | `5` | Analysis cycle period |
| `LOOKBACK_MINUTES` | ❌ | `60` | Rolling window kept in memory per instrument |
| `ORDER_MODE` | ❌ | `paper` | `signals_only` / `paper` / `live` |
| `ORDER_QTY_DEFAULT` | ❌ | `1` | Quantity when the signal doesn't specify |
| `SLIPPAGE_BPS` | ❌ | `5` | Paper-fill slippage |
| `SIGNAL_COOLDOWN_SECS` | ❌ | `300` | Min gap between identical signals |
| `MAX_OPEN_POSITIONS` | ❌ | `5` | Risk gate |
| `MAX_ORDER_NOTIONAL` | ❌ | `500000` | Risk gate, ₹ per order |
| `DAILY_LOSS_LIMIT` | ❌ | `10000` | Risk gate, ₹; halts new entries |
| `KILL_SWITCH_FILE` | ❌ | `KILL_SWITCH` | Path; existence halts entries |
| `PERSIST_TO_INFLUX` | ❌ | `true` | Write signals/orders/positions to `signals_YYYYMMDD` |
| `UPSTOX_ACCESS_TOKEN` | live only | — | Daily token (viz_hedge `generate_token.py`) |
| `UPSTOX_ORDER_SANDBOX` | ❌ | `false` | Route live orders to Upstox sandbox |
| `ENGINE_START` / `ENGINE_STOP` | ❌ | `09:16` / `15:30` | IST run window |
| `LOG_LEVEL` | ❌ | `INFO` | DEBUG / INFO / WARNING / ERROR |

---

## 12. Daily Runbook (Development)

```bash
# 0. viz_hedge feeder must be running (it populates tick_data_YYYYMMDD)

# 1. Copy the feeder's instrument list
#    ANALYZE_INSTRUMENTS = viz_hedge's SUBSCRIBE_INSTRUMENTS (or a subset)

# 2. (live mode only) ensure today's UPSTOX_ACCESS_TOKEN is in .env

# 3. Start the engine (waits for 09:16 IST, stops 15:30, flattens paper positions)
python run.py
```

Emergency stop: `touch KILL_SWITCH` (blocks new entries) or Ctrl-C (graceful shutdown,
journals final position state).

---

## 13. Cloud Deployment & Market-Hours Scheduling

viz_signals runs unattended in the cloud, co-located with the feeder. It follows the
pattern already established in
`../viz_hedge/docs/shaaru-aureus-cloud-deployment-runbook.md` — do not build parallel
infrastructure for it.

**Topology (v1): one VM runs both apps; data lives in InfluxDB Cloud.**

```
        InfluxDB Cloud (managed — existing account)
          ▲ writes (feeder)        ▲ reads + writes (engine)
          │                        │
EC2 t3.small (Ubuntu 24.04, ap-south-1 Mumbai, clock = Asia/Kolkata)
├── viz_hedge  feeder   (systemd, 09:05 → self-stops 15:28)
├── viz_signals engine  (systemd, 09:10 → self-stops 15:30)
└── dashboard API (§14 Phase B) — the only inbound-exposed surface
```

InfluxDB is **not** self-hosted — both apps point at the existing InfluxDB Cloud
account (`INFLUX_URL` = the cloud endpoint). Consequences:

- **Latency budget.** Every poll cycle issues one Flux query per instrument over the
  internet. With N instruments, a cycle costs roughly N × RTT to the InfluxDB Cloud
  region — keep the VM in the region nearest the account's region, and if cycles start
  overrunning `POLL_INTERVAL_SECS`, either raise the interval or batch the per-instrument
  queries into a single Flux query across measurements (noted as an optimization in §15).
- **Plan limits.** The platform creates buckets daily (`tick_data_YYYYMMDD` from the
  feeder, `signals_YYYYMMDD` from the engine). InfluxDB Cloud's **free plan caps buckets
  (2) and retention (~30 days)** — the daily-bucket pattern needs the usage-based plan.
  Either way, daily buckets accumulate: set a retention rule or a periodic cleanup once
  the EOD S3 export (feeder gap #4) lands.
- **Stateless VM.** Nothing durable lives on the box — it can be rebuilt or stopped
  without data loss.

The engine itself is I/O-light (Flux queries + pandas over a rolling window); one
`t3.small` handles both apps at current instrument counts; move up if the subscription
list grows past a few hundred.

**Scheduling.** The daily rhythm reuses the feeder's systemd timers, adding one unit:

| IST | Unit | What happens |
|---|---|---|
| 08:40 | `vizhedge-prep` (existing) | Instrument master, subscription list, Upstox token via `auto_token.py` |
| 09:05 | `vizhedge-feeder` (existing) | Feeder starts; self-gates to 09:15, self-stops 15:28 |
| 09:10 | `vizsignals-engine` (new) | Engine starts; self-gates to `ENGINE_START` 09:16, self-stops `ENGINE_STOP` 15:30, exit 0 |

The engine service mirrors the feeder unit: `Type=simple`, `EnvironmentFile=` pointing
at viz_signals' `.env`, `Restart=on-failure` (clean exit-0 at 15:30 does not loop),
`OnCalendar=Mon..Fri 09:10`, `After=vizhedge-feeder.service`. Because the app already
gates itself by IST market hours, the timer only needs to be "roughly before open" —
crash-restart mid-session re-enters the running window naturally.

**Shared daily token.** Live order mode needs the same daily Upstox token the feeder's
prep job already refreshes. Point the engine at it rather than running a second login
flow: either symlink/read the feeder's `.env` value into viz_signals' environment in the
unit file, or have `auto_token.py` write both `.env` files. Same applies to
`NSE_JSON_PATH` → the feeder's `data/NSE.json`.

**Known shared gap — NSE holiday calendar.** Mon–Fri timers still fire on exchange
holidays; both apps start, find no data/closed market, and idle out. Harmless but noisy.
The fix (one holiday-calendar check in the prep job that both apps honor) is tracked in
§15 and belongs in viz_hedge's prep step.

**Cost & lifecycle.** An always-on `t3.small` is ~US$15–20/month. Because the VM is
stateless (all data in InfluxDB Cloud), scheduled instance start/stop becomes a safe
option: EventBridge Scheduler starts it ~08:30 IST and stops it ~16:00 IST on weekdays,
cutting compute cost ~70%. This pairs with Grafana Cloud for Phase A (§14), which reads
InfluxDB Cloud directly — EOD review works even while the VM is off. Start always-on
for simplicity; add the schedule once the daily rhythm is proven. Containerization
(Docker + K8s) stays on the same roadmap track as the feeder — adopt when Kafka and
other platform pods arrive, so everything shares one orchestration layer.

---

## 14. Front-End (Monitoring Dashboard)

An operator-facing front-end showing, per instrument: live price, generated signals,
orders taken (and rejected, with reasons), open positions with running P&L, and the
day's realized results.

**Form factor decision: responsive web app, not native iOS/Android.** One codebase,
works on desktop and phone, installable as a PWA from the browser, no app-store
overhead, and it can ship weeks earlier. A native app adds cost without adding
information; revisit only if push-notification latency ever becomes a hard requirement
(PWA/web push covers most of it).

Delivery is phased so there is visibility from day one:

### Phase A — Grafana (day one, zero code)

Everything the engine knows is already persisted to InfluxDB Cloud (`signals_YYYYMMDD`
measurements `signal`, `order`, `position` — §10) alongside the tick data. Use
**Grafana Cloud (free tier)** with InfluxDB Cloud as a data source — nothing to host on
the VM, works from a phone browser, and dashboards stay available after hours or when
the VM is stopped. Panels: signal feed (table), unrealized P&L per open position (time
series), realized P&L cumulative (stat), order/rejection tables, LTP charts with signal
markers overlaid. This is the interim dashboard while Phase B is built — and remains
useful afterwards for ad-hoc queries. (Self-hosting Grafana on the VM works too, but
ties dashboard availability to the VM's lifecycle.)

### Phase B — Dedicated web dashboard

```
Browser (React/Next.js PWA)
   │  HTTPS + auth
   ▼
Dashboard API (FastAPI)
   │  reads only persisted state — never touches the engine process
   ├── InfluxDB   signals_YYYYMMDD + tick_data_YYYYMMDD   (live queries)
   └── journal/YYYYMMDD/*.jsonl                            (audit detail)
```

**Isolation principle** (mirrors architecture doc §7: trading engine minimally
exposed): the front-end reads *persisted state only*. The engine process has no
inbound interface; killing or redeploying the dashboard can never affect trading.
The one deliberate exception: an authenticated **"halt entries" button** that creates
the `KILL_SWITCH` file (§8) — the mechanism already exists and is inherently safe
(halt-only; resuming requires deleting the file deliberately).

**Screens (v1):**

| Screen | Contents |
|---|---|
| Watchlist | All instruments: LTP, day change, last signal + age, position badge, sparkline |
| Signal feed | Chronological signals: action, price, strategy, reason, eligibility outcome |
| Positions & P&L | Open positions: side, qty, entry, last, unrealized P&L, MFE/MAE; day totals (realized + unrealized) |
| Order blotter | All orders and fills, including risk-gate rejections with reasons |
| Risk panel | Kill-switch state (+ halt button), open-position count vs limit, daily loss vs limit, engine heartbeat |
| EOD summary | Closed trades: entry/exit, holding time, realized P&L, MFE/MAE per trade |

**API sketch** (FastAPI, new top-level `api/` in viz_signals or a separate
`viz_dashboard` repo once it grows): `GET /instruments`, `GET /signals?since=`,
`GET /positions`, `GET /orders`, `GET /pnl/summary`, `POST /kill-switch` (auth-gated),
plus a WebSocket `/stream` pushing deltas the server derives by polling InfluxDB every
few seconds. Engine heartbeat = age of the newest `position`/`signal` point vs poll
interval.

**Exposure & auth:** the dashboard is the only inbound surface on the VM. Two options,
in order of preference for v1: (a) keep it fully private behind **Tailscale/WireGuard**
— zero public exposure, works from your phone; (b) public via nginx + Let's Encrypt TLS
with single-user auth (HTTP basic → JWT later). Never expose InfluxDB or the engine
directly.

---

## 15. Known Gaps / Next Steps

1. **Algorithm integration** — replace `ExampleSmaCross` with the proprietary algorithm (owner, week of 2026-07-06)
2. **Cloud deployment** — provision per §13: `vizsignals-engine` systemd unit + timer on the feeder's VM
3. **Front-end Phase A** — Grafana panels over `signals_YYYYMMDD` (§14); then Phase B web dashboard
4. **Live fill stream** — Upstox portfolio WebSocket for real-time order/fill updates instead of polling
5. **Restart recovery** — rebuild open positions from `journal/` on startup
6. **Kafka** — consume `market-data`, publish `orders`/`executions` once the topics exist (per architecture §5.1)
7. **Multi-strategy aggregation** — signal weighting across strategies (architecture §5.2 Signal Generator)
8. **Position sizing** — confidence/vol-based sizing instead of fixed qty
9. **NSE holiday calendar** — shared prep-job check so timers don't start both apps on exchange holidays (§13)
10. **Containerize** — Dockerfile + K8s manifest, same track as the feeder (§13)
