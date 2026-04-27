import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Use relative asset paths so the built app works when served through
  // HA ingress (https://ha-host/api/hassio_ingress/TOKEN/). With the default
  // base '/', Vite outputs <script src="/assets/index.js"> which the browser
  // resolves to HA's root (404). With './' the browser resolves assets
  // relative to the current page URL, routing them through the ingress proxy.
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://localhost:8099',
      '/ws': { target: 'ws://localhost:8099', ws: true },
    },
  },
})
