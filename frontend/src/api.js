import { useEffect, useRef, useState } from 'react'

export async function get(path, params) {
  const q = params ? '?' + new URLSearchParams(params) : ''
  const r = await fetch(`/api${path}${q}`)
  if (!r.ok) throw new Error(`${path} → ${r.status}`)
  return r.json()
}

/** Poll a REST endpoint. Used for things that change slowly (the Home table). */
export function usePoll(path, params, everyMs = 5000) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const key = JSON.stringify(params || {})
  useEffect(() => {
    let alive = true, timer
    const tick = () => get(path, params)
      .then(d => { if (alive) { setData(d); setErr(null) } })
      .catch(e => { if (alive) setErr(e.message) })
      .finally(() => { if (alive) timer = setTimeout(tick, everyMs) })
    tick()
    return () => { alive = false; clearTimeout(timer) }
  }, [path, key, everyMs])
  return { data, err }
}

/**
 * Live bars over one WebSocket, shared by every component.
 *
 * Reconnects with backoff — an iPad suspends sockets the moment you switch apps or
 * lock it, so a dashboard without reconnect is blank every time you come back.
 */
export function useLiveBars() {
  const [bars, setBars] = useState({})       // key -> latest bar
  const [signals, setSignals] = useState([])
  const [status, setStatus] = useState('connecting')
  const ref = useRef({ retry: 0 })

  useEffect(() => {
    let sock, timer, alive = true
    const open = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      sock = new WebSocket(`${proto}://${location.host}/ws`)
      sock.onopen = () => { if (alive) { setStatus('live'); ref.current.retry = 0 } }
      sock.onclose = () => {
        if (!alive) return
        setStatus('reconnecting')
        const wait = Math.min(15000, 1000 * 2 ** ref.current.retry++)
        timer = setTimeout(open, wait)
      }
      sock.onmessage = ev => {
        const m = JSON.parse(ev.data)
        if (m.type === 'bars') {
          setBars(prev => {
            const next = { ...prev }
            for (const b of m.data) next[b.key] = b
            return next
          })
        } else if (m.type === 'signal') {
          setSignals(prev => [m.data, ...prev].slice(0, 50))
        }
      }
    }
    open()
    return () => { alive = false; clearTimeout(timer); sock && sock.close() }
  }, [])

  return { bars, signals, status }
}
