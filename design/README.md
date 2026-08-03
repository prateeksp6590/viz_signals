# Dashboard prototype — design notes

`api/static/prototype.html` is a **static mock with fabricated data**. It exists to
settle layout and information hierarchy before any of it is wired to the API. Open it
on the iPad through the tunnel:

```
ssh -i data_feeder_kp.pem -N -L 8000:localhost:8000 ubuntu@<ec2-dns>
# then on the iPad, over Tailscale:  http://<tailscale-ip>:8000/static/prototype.html
```

Tap the **segment pills** (NSE/BSE/MCX) to toggle a simulated after-hours state — that
is how you check the 15:30 rule without waiting for 15:30.

## Decisions baked in, and why

**Broker identity is a strip, not a panel.** Client ID, name, date/day/time and segment
status are glanceable facts you never interact with. On an iPad, giving them a card each
would cost a third of the fold for information you read once.

**Segment status is derived from the clock, not from the feed.** NSE/BSE grey out at
15:30 and MCX stays green to 23:30. If it were derived from "are ticks arriving", a
feeder outage would look identical to a market close — the two failures need to be
distinguishable at a glance.

**Service health is three states, and yellow is the important one.** Green/red is easy;
the case that actually happened on 2026-08-03 was *running but wrong* — the engine alive
while cycles overran the poll interval, and the feeder alive while every write 401'd.
Those are yellow, and yellow needs a reason string next to it.

**The instruments table is the whole Home tab.** No summary cards above it. Totals belong
in one row of the table footer; a "today's P&L" card would just repeat it larger.

**Trend is a 5-level badge, not a number.** Arrows plus colour survive being read at
arm's length on a stand, which a slope value does not.

**Explorer charts are stacked and share one x-axis** — LTP, LTQ, VTT, then angle vs
threshold. Stacking beats a grid here because every question is "what happened at this
moment", answered by reading down a vertical line.

**Instrument selection cascades segment → instrument**, matching the Grafana variables,
so the same mental model works in both places.

## Deliberately deferred

- iPhone/Android layout. The table is the problem: nine columns cannot survive 390px.
  It likely becomes a card list, which is a different component, not a media query.
- Order entry. Read-only until the strategy is profitable on paper.
- Drawing/annotation on charts.
- Multi-day comparison in the explorer.

## Open questions for the next pass

1. Trend needs a definition. Candidates: sign+magnitude of `slope_recent`, angle vs its
   own threshold, or an N-bar return. It should probably be the same quantity the
   strategy acts on, or the table will disagree with the signals.
2. Is P&L per instrument realised-only, or realised + open? Today's loss-limit bug came
   from exactly that ambiguity.
3. Should "Status" distinguish *never ticked today* from *stopped ticking 10 min ago*?
   For MCX pre-open the first is normal; for NIFTY mid-session the second is an incident.
