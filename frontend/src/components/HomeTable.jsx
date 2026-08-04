const fmt = (v, d = 2) => v == null ? '—' : Number(v).toLocaleString('en-IN',
  { minimumFractionDigits: d, maximumFractionDigits: d })
const compact = v => v == null ? '—' : v >= 1e5 ? (v / 1e5).toFixed(1) + 'L'
  : v >= 1e3 ? (v / 1e3).toFixed(0) + 'k' : String(Math.round(v))

function Trend({ t }) {
  const arrow = { 2: '▲▲', 1: '▲', 0: '—', '-1': '▼', '-2': '▼▼' }[t.level] ?? '—'
  const cls = { 2: 't2', 1: 't1', 0: 't0', '-1': 'tm1', '-2': 'tm2' }[t.level] ?? 't0'
  // the components are the tooltip: a badge nobody can interrogate stops being trusted
  const tip = t.ready
    ? `persistence ${t.persistence} · move ${t.ret_pct}% · ${t.move_z}σ`
    : 'warming up — not enough ticks yet'
  return <span className={`pill ${cls}`} title={tip}>{arrow} {t.label}</span>
}

export default function HomeTable({ home, bars }) {
  if (!home) return <div className="mut">loading…</div>
  const { rows, totals } = home
  return (
    <>
      <div className="wrap">
        <table>
          <thead><tr>
            <th style={{ width: 36 }}>#</th><th>Instrument</th><th>Status</th>
            <th className="n">LTP</th><th className="n hide-s">LTQ</th>
            <th className="n hide-s">VTT</th><th>Trend</th>
            <th className="hide-s">Recent trigger</th><th className="n">P&amp;L</th>
          </tr></thead>
          <tbody>
            {rows.map(r => {
              // the websocket bar is fresher than the 5s REST poll
              // websocket bar > 5s poll > day close. A stale price is dimmed and
              // labelled 'close' so it is never mistaken for a live quote.
              const ltp = bars[r.key]?.c ?? r.ltp
              const p = r.pnl?.total
              const trig = r.trigger
              return (
                <tr key={r.key} style={r.traded ? undefined : { opacity: .45 }}
                    title={r.traded ? undefined
                      : `${r.moneyness} — stored but not traded (ANALYZE_MONEYNESS)`}>
                  <td className="mut">{r.sr}</td>
                  <td>{r.symbol}
                    {r.moneyness && r.moneyness !== 'UNKNOWN' &&
                      <span className="mut" style={{ fontSize: 10, marginLeft: 6 }}>
                        {r.moneyness}</span>}
                  </td>
                  <td>
                    <span className="pill" style={{
                      background: r.status === 'live' ? 'rgba(63,185,80,.12)' : 'rgba(139,148,158,.15)',
                      color: r.status === 'live' ? 'var(--grn)' : 'var(--dim)' }}
                      title={r.age_s != null ? `last tick ${r.age_s}s ago` : 'no ticks today'}>
                      <i className={`dot ${r.status === 'live' ? 'on' : 'off'}`}
                         style={{ width: 6, height: 6 }} />{r.status}
                    </span>
                  </td>
                  <td className={`n ${r.stale && !bars[r.key] ? 'mut' : ''}`}
                      title={r.stale && !bars[r.key] ? 'session close — not a live quote' : ''}>
                    {fmt(ltp)}{r.stale && !bars[r.key] &&
                      <span style={{ fontSize: 10, marginLeft: 4, opacity: .7 }}>close</span>}
                  </td>
                  <td className="n hide-s mut">{r.ltq == null ? '—' : Math.round(r.ltq)}</td>
                  <td className="n hide-s mut">{compact(r.vtt)}</td>
                  <td><Trend t={r.trend} /></td>
                  <td className="hide-s">{trig
                    ? <span title={`angle ${trig.angle?.toFixed?.(2)} ≥ ${trig.threshold?.toFixed?.(2)}`}>
                        {String(trig.t).slice(11, 19)} {trig.action?.replace('ENTER_', '')}
                      </span>
                    : <span className="mut">—</span>}</td>
                  <td className={`n ${p == null ? 'mut' : p >= 0 ? 'pos' : 'neg'}`}
                      title={r.pnl ? `realised ${fmt(r.pnl.realised, 0)} + open ${fmt(r.pnl.open, 0)}` : ''}>
                    {p == null ? '—' : (p >= 0 ? '+' : '') + fmt(p, 0)}
                  </td>
                </tr>)
            })}
          </tbody>
          <tfoot><tr>
            <td colSpan={8} style={{ textAlign: 'right', color: 'var(--dim)' }}>
              Day total (realised {fmt(totals.realised, 0)} + open {fmt(totals.open, 0)})
            </td>
            <td className={`n ${totals.total >= 0 ? 'pos' : 'neg'}`}>
              <b>{(totals.total >= 0 ? '+' : '') + fmt(totals.total, 0)}</b>
            </td>
          </tr></tfoot>
        </table>
      </div>
    </>
  )
}
