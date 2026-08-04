# MCX poller

Replaces the websocket for commodities. Polls Upstox market-quote every 2 minutes,
writes to the same `tick_data` bucket and schema, and evaluates signals on 5-minute
bars. Signals go to the log, the journal and Telegram — **not** the dashboard.

## Why polling

CRUDEOILM produced ~4,750 ticks in a session against ~34,000 for a NIFTY leg, and
SILVERM managed 17 on 31 July. A streaming subscription for that is overkill, and the
1,081-tick warm-up the tick strategy needs is unreachable at those rates.

## This is a different strategy

`MCX_N1`/`MCX_N2` count **2-minute samples**, not ticks. `5/8` is a 10- and 16-minute
geometry. The NSE calibration (`q=0.95` on tick data, 2-second latency sensitivity)
carries no evidence here — the MCX journal has to prove itself on its own.

`MCX_Q=0.90` is looser than NSE's 0.95 deliberately: at ~450 samples a session there
are far fewer observations, so a 95th percentile would fire a handful of times a week.

## Liquid strikes, not ATM

`discover()` quotes ATM ± `MCX_DISCOVER_RANGE` once at startup and keeps the
`MCX_TOP_K` strikes by **traded volume**. On MCX the busiest strike is often a round
number some distance from spot, so assuming ATM would pick the wrong ones. The nearest
future for each underlying is added too — it leads the options.

## Install

```bash
sudo cp deploy/vizmcx.service deploy/vizmcx.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vizmcx.timer
```

Then take MCX off the websocket, in `viz_hedge/.env`:

```ini
CHAIN_UNDERLYINGS=NIFTY:5 SENSEX:5        # MCX removed
```

and out of the tick strategy, in `viz_signals/.env`:

```ini
ANALYZE_SEGMENTS=NSE_FO,BSE_FO
```

## Night analysis

```bash
python utils/check_signals.py --date YYYYMMDD          # NSE/BSE
cat journal/YYYYMMDD/mcx_signals.jsonl | wc -l         # MCX count
```

MCX signals are journaled separately (`mcx_signals.jsonl`) so they never contaminate
the NSE/BSE statistics — the two run on different data, cadence and parameters, and
pooling them would make both unreadable.
