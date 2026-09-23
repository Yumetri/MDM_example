import { defineConfig, devices } from '@playwright/test'

if (!process.env.MDM_E2E_ORIGIN || !process.env.MDM_E2E_CONTROL_KEY) {
  throw new Error('Use make frontend-e2e to start the isolated browser test API.')
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: 'list',
  use: {
    baseURL: process.env.MDM_E2E_ORIGIN,
    ignoreHTTPSErrors: true,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
