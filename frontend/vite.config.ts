import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'
import { readFileSync } from 'node:fs'

const apiOrigin = process.env.MDM_FRONTEND_API_ORIGIN ?? 'http://127.0.0.1:8000'
const upstream = new URL(apiOrigin)
if (upstream.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(upstream.hostname)) {
  throw new Error('The local frontend proxy requires a loopback HTTP API.')
}
const tlsKey = process.env.MDM_FRONTEND_TLS_KEY
const tlsCert = process.env.MDM_FRONTEND_TLS_CERT
if (Boolean(tlsKey) !== Boolean(tlsCert)) throw new Error('Both local HTTPS key and certificate are required.')
const https = tlsKey && tlsCert ? { key: readFileSync(tlsKey), cert: readFileSync(tlsCert) } : undefined

export default defineConfig({
  plugins: [react()],
  server: {
    host: 'localhost',
    strictPort: true,
    https,
    proxy: {
      '/api/v1': {
        target: apiOrigin,
        changeOrigin: false,
      },
    },
  },
  preview: { host: 'localhost', strictPort: true, https },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    clearMocks: true,
    restoreMocks: true,
  },
})
