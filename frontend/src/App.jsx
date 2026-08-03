import { useState } from 'react'
import { usePoll, useLiveBars } from './api'
import Header from './components/Header'
import Services from './components/Services'
import HomeTable from './components/HomeTable'
import Explorer from './components/Explorer'

export default function App() {
  const [tab, setTab] = useState('home')
  const { data: health } = usePoll('/health', null, 10000)
  const { data: home, err } = usePoll('/home', { lookback: 30 }, 5000)
  const { bars, status } = useLiveBars()

  return (
    <>
      <Header health={health} />
      <Services health={health} wsStatus={status} home={home} />
      <div className="tabs">
        {[['home', 'Home'], ['expl', 'Data Explorer']].map(([id, label]) =>
          <button key={id} className={`tab ${tab === id ? 'on' : ''}`}
                  onClick={() => setTab(id)}>{label}</button>)}
      </div>
      <div className="pane">
        {err && <div className="err">API unreachable — {err}</div>}
        {tab === 'home'
          ? <HomeTable home={home} bars={bars} />
          : <Explorer home={home} />}
      </div>
    </>
  )
}
