import { defineConfig, devices } from "@playwright/test";

import { E2E_DEV_USER } from "./e2e/devUser.ts";
import { READER_BASE_URI } from "./e2e/readerRecords.ts";

// The e2e suite runs the production build (`pnpm build` first) in `vite preview` against a real
// `lae api` process, which Playwright starts with the development login bypass (LAE_DEV_USER) and
// LAE_ENV=test. The preview server proxies the API paths to it (vite.config.ts), so the browser
// sees one origin as in production.
//
// The API needs a migrated database: set LAE_DATABASE_URL (`uv run lae migrate` first);
// e2e/globalSetup.ts stores the published records the Public Reader tests open. Ports:
// LAE_E2E_PORT for the preview server (default 4173) and LAE_E2E_API_PORT for the API (default
// 8000); agents use their slot's ports (docs/agent-workflow.md §3).
const port = Number(process.env.LAE_E2E_PORT ?? "4173");
const apiPort = Number(process.env.LAE_E2E_API_PORT ?? "8000");
const baseURL = `http://127.0.0.1:${String(port)}`;
const apiURL = `http://127.0.0.1:${String(apiPort)}`;
const ci = Boolean(process.env.CI);

export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/globalSetup.ts",
  fullyParallel: true,
  forbidOnly: ci,
  retries: 0,
  reporter: ci ? [["github"], ["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run --project ../backend --locked lae api",
      env: {
        LAE_ENV: "test",
        LAE_DEV_USER: `${E2E_DEV_USER.email}:${E2E_DEV_USER.role}`,
        LAE_API_HOST: "127.0.0.1",
        LAE_API_PORT: String(apiPort),
        LAE_BASE_URI: READER_BASE_URI,
      },
      url: `${apiURL}/health/live`,
      reuseExistingServer: !ci,
      stdout: "pipe",
      timeout: 60_000,
    },
    {
      command: `corepack pnpm preview --host 127.0.0.1 --port ${String(port)} --strictPort`,
      env: { VITE_API_TARGET: apiURL },
      url: baseURL,
      reuseExistingServer: !ci,
      timeout: 30_000,
    },
  ],
});
