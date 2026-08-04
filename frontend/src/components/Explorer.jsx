import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'
import { get } from '../api'

const OPTS = {
  layout: { background: { color: 'transparent' }, textColor: '#8b949e', fontSize: 10 },
  grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
  rightPriceScale: { borderColor: '#30363d' },
  timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
  crosshair: { mode: 0 },
  handleScale: { axisPressedMouseMove: true },
}

/** Each panel owns its axis — they are stacked for scanning, not linked. */
function Panel({ title, data, type = 'line', color = '#58a6ff', height = 150 }) {
  const box = useRef(null), chart = useRef(null), series = useRef(null)
  useEffect(() => {
    if (!box.current) return
    chart.current = createChart(box.current, { ...OPTS, height })
    series.current =
      type === 'candle'
        ? chart.current.addCandlestickSeries({
            upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
            wickUpColor: '#26a69a', wickDownColor: '#ef5350' })
        : type === 'hist'
          ? chart.current.addHistogramSeries({ color })
          : chart.current.addLineSeries({ color, lineWidth: 2 })
    const ro = new ResizeObserver(() => chart.current?.applyOptions(
      { width: box.current.clientWidth }))
    ro.observe(box.current)
    return () => { ro.disconnect(); chart.current?.remove() }
  }, [type, color, height])
  useEffect(() => { if (series.current && data) series.current.setData(data) }, [data])
  return <div className="chart"><h4>{title}</h4><div ref={box} /></div>
}

export default function Explorer({ home }) {
  const rows = home?.rows || []
  const segments = useMemo(() => [...new Set(rows.map(r => r.segment))].sort(), [rows])
  const [seg, setSeg] = useState('')
  const [key, setKey] = useState('')
  const [mins, setMins] = useState(60)
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => { if (!seg && segments.length) setSeg(segments[0]) }, [segments, seg])
  const inSeg = rows.filter(r => r.segment === seg)
  useEffect(() => { if (inSeg.length && !inSeg.some(r => r.key === key)) setKey(inSeg[0].key) },
            [seg, rows]) // eslint-disable-line

  useEffect(() => {
    if (!key) return
    let alive = true
    setErr(null)
    Promise.all([
      get('/ticks', { key, minutes: mins, bucket: '30s' }),
      get('/signals', { symbol: rows.find(r => r.key === key)?.symbol, limit: 10 }),
    ]).then(([t, s]) => { if (alive) setD({ t, s }) })
      .catch(e => alive && setErr(e.message))
    return () => { alive = false }
  }, [key, mins]) // eslint-disable-line

  const bars = d?.t?.bars || []

  // lightweight-charts renders every timestamp in UTC and has no timezone option, so
  // an IST time would display 5h30m early. Shifting the epoch by the offset makes the
  // axis read IST — the standard workaround, and why these are not raw UTC seconds.
  const IST_OFFSET = 5.5 * 3600
  const toSec = s => Math.floor(new Date(s).getTime() / 1000) + IST_OFFSET

  const candles = bars
    .filter(b => b.close != null)
    .map(b => ({ time: toSec(b._time), open: b.open, high: b.high, low: b.low, close: b.close }))
  const vol = bars.map(b => ({
    time: toSec(b._time), value: b.volume ?? 0,
    color: b.close >= b.open ? '#26a69a88' : '#ef535088' }))
  const ltq = bars.filter(b => b.ltq != null).map(b => ({ time: toSec(b._time), value: b.ltq }))
  const vtt = bars.filter(b => b.vtt != null).map(b => ({ time: toSec(b._time), value: b.vtt }))

  return (
    <div>
      <div className="ctl">
        <select value={seg} onChange={e => setSeg(e.target.value)}>
          {segments.map(s => <option key={s}>{s}</option>)}
        </select>
        <select value={key} onChange={e => setKey(e.target.value)} style={{ minWidth: 250 }}>
          {inSeg.map(r => <option key={r.key} value={r.key}>{r.symbol}</option>)}
        </select>
        <span style={{ flex: 1 }} />
        {[60, 180, 375].map(m =>
          <button key={m} className={`btn ${mins === m ? 'on' : ''}`}
                  onClick={() => setMins(m)}>{m === 375 ? 'Full day' : m + 'm'}</button>)}
      </div>

      {err && <div className="err">{err}</div>}

      <Panel title="LTP — 30s candles" data={candles} type="candle" height={200} />
      <Panel title="Volume per bar (VTT difference)" data={vol} type="hist"
             color="#8b949e" height={110} />
      <Panel title="LTQ — last traded qty (bar mean)" data={ltq} color="#a371f7" height={110} />
      <Panel title="VTT — cumulative volume" data={vtt} color="#d29922" height={110} />

      <div className="wrap">
        <table style={{ minWidth: 620 }}>
          <thead><tr><th>Time</th><th>Action</th><th className="n">Price</th>
            <th className="n">Angle</th><th className="n hide-s">Threshold</th>
            <th className="n">Ratio</th></tr></thead>
          <tbody>
            {(d?.s || []).slice(0, 10).map((s, i) => {
              const a = s.meta?.angle_deg, t = s.meta?.threshold_deg
              const ratio = a && t ? a / t : null
              return (
                <tr key={i}>
                  <td className="mut">{String(s.ts).slice(11, 19)}</td>
                  <td><span className="pill t1">{s.action?.replace('ENTER_', '')}</span></td>
                  <td className="n">{Number(s.price).toFixed(2)}</td>
                  <td className="n">{a?.toFixed(2) ?? '—'}</td>
                  <td className="n hide-s mut">{t?.toFixed(2) ?? '—'}</td>
                  <td className={`n ${ratio >= 1.3 ? 'pos' : 'mut'}`}>{ratio?.toFixed(2) ?? '—'}</td>
                </tr>)
            })}
            {!(d?.s || []).length &&
              <tr><td colSpan={6} className="mut">no triggers for this instrument today</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
