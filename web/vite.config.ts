import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', chunkSizeWarningLimit: 900, assetsInlineLimit: 0 },
  server: { port: 5199, proxy: { '/api': 'http://127.0.0.1:8421' } },
})
