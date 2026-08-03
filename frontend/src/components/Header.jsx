import { useEffect, useState } from 'react'

// Segment state comes from the CLOCK, not from whether ticks are arriving. If it were
// tick-derived, a feeder outage would be indistinguishable from a market close.
const SESSIONS = { NSE: [555, 930], BSE: [555, 930], MCX: [555, 1410] }

export default function Header({ health }) {
  const [now, setNow] = useState(new Date())
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(t) }, [])
  const mins = now.getHours() * 60 + now.getMinutes()
  const day = now.toLocaleDateString('en-GB', { weekday: 'long' })
  const date = now.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })

  return (
    <div className="hdr">
      <div className="who">
        <b>Shaaru Aureus</b>
        <span>Client <b>{health?.client_id || '—'}</b></span>
        <span>{health?.client_name || 'Prateek Vishwakarma'}</span>
      </div>
      <div className="clock">
        <b>{day}</b> · {date} · <b>{now.toTimeString().slice(0, 8)}</b> IST
      </div>
      <div className="segs">
        {Object.entries(SESSIONS).map(([name, [a, b]]) => {
          const open = mins >= a && mins <= b
          return (
            <span className="seg" key={name} title={open ? 'open' : 'closed'}>
              <i className={`dot ${open ? 'on' : 'off'}`} />{name}
            </span>
          )
        })}
      </div>
    </div>
  )
}
