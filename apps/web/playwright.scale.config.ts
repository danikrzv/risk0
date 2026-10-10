import { defineConfig, devices } from "@playwright/test"

// Browser-level scale regression: the radar must stay fast and interactive on a
// live-sized topology (~20k entities, ~50k relationships, ~20k open incidents).
//
// Kept apart from the golden flows (playwright.config.ts): its own risk and web
// servers on their own ports, its own temp radar database, and its own spec
// directory, so the synthetic data never reaches the golden fixture radar.
// The database is seeded by services/risk/scripts/seed_synthetic_radar.py
// (app.radar.synthetic, written through the real repositories) before uvicorn
// starts. Fixture mode, no radar scheduler, and no network, as for the golden
// flows.

const RISK_PORT = Number(process.env.RISK0_E2E_SCALE_RISK_PORT ?? 8720)
const WEB_PORT = Number(process.env.RISK0_E2E_SCALE_WEB_PORT ?? 3720)
const RISK_URL = `http://127.0.0.1:${RISK_PORT}`
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`
// Set once in the runner (workers inherit it) so global-teardown.ts removes the
// same ~30 MB database the risk server used.
process.env.RISK0_E2E_SCALE_RADAR_DB_PATH ??= `/tmp/risk0-e2e-scale-radar-${process.pid}-${Date.now()}.sqlite3`
const RADAR_DB_PATH = process.env.RISK0_E2E_SCALE_RADAR_DB_PATH
const RISK_PROJECT = "../../services/risk"

export default defineConfig({
  testDir: "./e2e-scale",
  testMatch: "**/*.spec.ts",
  globalTeardown: "./e2e-scale/global-teardown.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
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
      command: [
        `uv run --project ${RISK_PROJECT} python ${RISK_PROJECT}/scripts/seed_synthetic_radar.py ${RADAR_DB_PATH}`,
        `uv run --project ${RISK_PROJECT} uvicorn app.main:app --app-dir ${RISK_PROJECT} --host 127.0.0.1 --port ${RISK_PORT}`,
      ].join(" && "),
      url: `${RISK_URL}/v1/health`,
      env: {
        RISK0_DATA_MODE: "fixture",
        RISK0_RADAR_DB_PATH: RADAR_DB_PATH,
        // Only the synthetic radar: no fixture topology, no scheduler writes.
        RISK0_RADAR_SEED_FIXTURES: "0",
        RISK0_RADAR_ENABLED: "0",
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
