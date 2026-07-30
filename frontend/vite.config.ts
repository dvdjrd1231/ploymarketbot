import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard is served by FastAPI from `dist/` in normal use. `npm run dev`
// proxies the API and the WebSocket to the Python server so the UI can be
// hot-reloaded while the engine keeps running.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true, ws: true },
      '/health': { target: 'http://127.0.0.1:8765', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
})
