import { defineConfig, devices } from "@playwright/test"

// Golden end-to-end flows against both real servers. The risk service runs in
// deterministic fixture mode (RISK0_DATA_MODE=fixture): recorded Morpho data and
// fixture quote, gas, price, oracle-health, and probability providers injected
// through the same FastAPI dependency wiring as live mode. No network is used.
//
// Browser: Playwright 1.56 runs headless Chromium on the `chromium-headless-shell`
// build (1194), so only that cached build is needed; `pnpm exec playwright
// install chromium-headless-shell` fetches it on a fresh machine.

const RISK_PORT = Number(process.env.RISK0_E2E_RISK_PORT ?? 8710)
const WEB_PORT = Number(process.env.RISK0_E2E_WEB_PORT ?? 3710)
const RISK_URL = `http://127.0.0.1:${RISK_PORT}`
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`
const RADAR_DB_PATH = `/tmp/risk0-e2e-radar-${process.pid}-${Date.now()}.sqlite3`

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: WEB_URL,
    headless: true,
    trace: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], headless: true } }],
  webServer: [
    {
      name: "risk",
      command: `uv run --project ../../services/risk uvicorn app.main:app --app-dir ../../services/risk --host 127.0.0.1 --port ${RISK_PORT}`,
      url: `${RISK_URL}/v1/health`,
      env: {
        RISK0_DATA_MODE: "fixture",
        RISK0_RADAR_DB_PATH: RADAR_DB_PATH,
        RISK0_RADAR_SEED_FIXTURES: "1",
        // Fixture mode never reads it; blank it so no live credential is in scope.
        ETHEREUM_RPC_URL: "",
      },
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      name: "web",
      command: `pnpm run build && pnpm exec next start --hostname 127.0.0.1 --port ${WEB_PORT}`,
      url: WEB_URL,
      env: { RISK_API_URL: RISK_URL },
      reuseExistingServer: false,
      timeout: 240_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
})
