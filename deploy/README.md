# Deploying viz_signals

## Does this need its own EC2?

**No — not yet.** Run it on the existing feeder box.

The two workloads do not compete for the same resource. viz_hedge is I/O-bound
(websocket in, HTTP out) and nearly idle on CPU. viz_signals is a 5-second poll
loop whose actual maths — an arctangent over 80 floats — is free; its cost is
memory for the rolling tick window.

Rough footprint at 100 instruments and `LOOKBACK_MINUTES=60`:

| | |
|---|---|
| rolling ticks held | ~100 × 3,600 rows |
| pandas resident | ~250–400 MB |
| CPU | single-digit %, spiking on resample |
| InfluxDB queries | 1 per cycle (batched), ~4.5k/day |

So the deciding factor is RAM, not CPU. Check what you have:

```bash
ssh -i data_feeder_kp.pem ubuntu@<ip> "free -m; nproc; curl -s http://169.254.169.254/latest/meta-data/instance-type"
```

- **t3.small (2 GB) or larger** → comfortable, colocate.
- **t3.micro (1 GB)** → tight. Either resize to t3.small, or cut
  `LOOKBACK_MINUTES` to 20 and trim `ANALYZE_INSTRUMENTS` to the handful you
  actually trade. Do not run both at 1 GB with a 60-minute window.

`vizsignals.service` sets `MemoryMax=700M`, `CPUQuota=60%` and
`OOMScoreAdjust=500`, so if memory ever runs short the kernel kills the signal
generator and leaves the feeder alone. That is the point of colocating safely.

### Split to a second instance when

1. you go to `ORDER_MODE=live` — real money deserves fault isolation;
2. you add strategies until CPU or RAM is genuinely the binding constraint;
3. you want to deploy/restart strategies without touching the feed.

Until one of those is true, a second box adds cost and a second thing to patch,
in exchange for isolation you can get from cgroups today.

## Install

```bash
scp -i data_feeder_kp.pem -r viz_signals ubuntu@<ip>:/home/ubuntu/
ssh -i data_feeder_kp.pem ubuntu@<ip>
cd /home/ubuntu/viz_signals
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env          # INFLUX_TOKEN, ANALYZE_INSTRUMENTS, ANGLE_*

sudo cp deploy/vizsignals.service deploy/vizsignals.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vizsignals.timer
systemctl list-timers vizsignals.timer     # confirm it is ARMED, not dormant
```

That last check matters: a timer that exists but was never `enable --now`d
silently never fires — the exact failure that left the feeder on a stale token.

## Watch it

```bash
journalctl -u vizsignals -f
journalctl -u vizsignals -n 100 --no-pager | grep SIGNAL
```

## Order of operations

1. `ORDER_MODE=signals_only` for at least a week. Signals are journaled to
   JSONL and InfluxDB; nothing is transmitted anywhere.
2. Compare those live signals against `backtest/backtest.py` over the same day.
   They must agree — both call `src/strategies/angle_math.py`. If they differ,
   the bug is in data plumbing, not in the maths.
3. `ORDER_MODE=paper` once they agree.
4. `ORDER_MODE=live` only after paper P&L survives a full expiry cycle.
