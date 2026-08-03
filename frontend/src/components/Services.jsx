/**
 * Three states, and yellow is the one that matters: "running but wrong" is the
 * failure that actually happens — the engine alive while cycles overrun, the feeder
 * alive while every write is rejected. Yellow always carries a reason.
 */
function Card({ label, state, sub }) {
  const cls = { ok: 'g', warn: 'y', bad: 'r' }[state] || 'r'
  const dot = { ok: 'on', warn: 'warn', bad: 'bad' }[state] || 'bad'
  return (
    <div className={`card ${cls}`}>
      <span className={`dot ${dot}`} />
      <div><div className="lbl">{label}</div><div className="sub">{sub}</div></div>
    </div>
  )
}

export default function Services({ health, wsStatus, home }) {
  const live = home?.live ?? 0, total = home?.n ?? 0
  const feeder = total === 0 ? 'bad' : live === 0 ? 'bad' : live < total * 0.8 ? 'warn' : 'ok'
  return (
    <div className="svc">
      <Card label="Feeder" state={feeder}
            sub={total ? `${live}/${total} instruments live` : 'no data'} />
      <Card label="Signal Generator" state={health ? 'ok' : 'bad'}
            sub={health ? `mode ${health.mode}` : 'unreachable'} />
      <Card label="InfluxDB" state={health?.hub_error ? 'warn' : health ? 'ok' : 'bad'}
            sub={health?.hub_error ? health.hub_error.slice(0, 46) : `bucket ${health?.bucket || '—'}`} />
      <Card label="Live stream" state={wsStatus === 'live' ? 'ok' : 'warn'}
            sub={`websocket ${wsStatus} · ${health?.clients ?? 0} client(s)`} />
    </div>
  )
}
