# UI

React + Vite, built into `../api/static/app` and served by FastAPI. One origin for API
and UI: no CORS, no second web server to run or secure.

## Develop (laptop)

Keep the SSH tunnel open so the API is on localhost:8000, then:

```bash
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Vite proxies `/api` and `/ws` to the tunnel, so you get hot reload against **live EC2
data** without deploying.

## Deploy

`api/static/app` is a COMMITTED BUILD ARTIFACT. Editing `src/` changes nothing until
you rebuild — and the failure is silent, the dashboard just keeps its old behaviour.

```bash
cd frontend && npm run build          # -> ../api/static/app
cd .. && git add -A                   # from the PARENT: output is outside frontend/
git commit -m "rebuild frontend" && git push
# on EC2:
git pull && sudo systemctl restart vizapi
```

`/api/health` returns `stale_bundle` with a message whenever `src/` is newer than the
built `index.html`, so a forgotten rebuild shows up as a fact rather than a mystery.

Then `http://<tailscale-ip>:8000/` redirects to `/app/`. On iPad, Share → **Add to Home
Screen** installs it: fullscreen, own icon, no address bar.

## Choices

**lightweight-charts, not Chart.js.** It is TradingView's, ~45 kB, built for financial
time series, and stays smooth on an iPad where Chart.js struggles past a few thousand
points.

**Bars over the websocket, REST for the table.** The table's shape changes slowly (5s
poll); prices change constantly (websocket). `HomeTable` prefers the websocket LTP and
falls back to the polled one, so a socket drop degrades rather than blanks.

**Reconnect with backoff.** iPadOS suspends websockets on app switch or lock — without
reconnect the dashboard is blank every time you come back to it.

**API responses are never cached by the service worker.** A PWA showing a stale LTP is
worse than showing nothing.
