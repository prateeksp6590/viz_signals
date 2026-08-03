import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// Built assets land in ../api/static/app, which FastAPI serves. One origin for the
// API and the UI means no CORS and no second web server to run or secure.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Shaaru Aureus', short_name: 'Aureus',
        theme_color: '#0d1117', background_color: '#0d1117',
        display: 'standalone', orientation: 'any', start_url: '/app/',
        icons: [{ src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
                { src: 'icon-512.png', sizes: '512x512', type: 'image/png' }]
      },
      // Never cache API responses — a trading UI showing a stale LTP from the
      // service worker is worse than showing nothing.
      workbox: { navigateFallbackDenylist: [/^\/api/, /^\/ws/] }
    })
  ],
  base: '/app/',
  build: { outDir: '../api/static/app', emptyOutDir: true },
  server: {
    port: 5173,
    // dev: laptop runs Vite, API stays on EC2 through the SSH tunnel
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true }
    }
  }
})
