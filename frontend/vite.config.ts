import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  base: '/static/',
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': { target: process.env.DEAL_TRACKER_API || 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
})
