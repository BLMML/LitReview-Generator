import { defineConfig } from '@playwright/test'
const live = process.env.M12_LIVE_TEST === '1'
export default defineConfig({
  testDir: './tests/e2e',
  testMatch: live ? 'live.spec.ts' : 'ui.spec.ts',
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: 'list',
  use: {
    baseURL: live ? 'http://127.0.0.1:5174' : 'http://127.0.0.1:5173',
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `npm run dev -- --port ${live ? 5174 : 5173}`,
    url: live ? 'http://127.0.0.1:5174' : 'http://127.0.0.1:5173',
    reuseExistingServer: !live,
    timeout: 60_000,
  },
})
