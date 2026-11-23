import { defineConfig, devices } from "@playwright/test";

import { E2E_DEV_USER } from "./e2e/devUser.ts";
import { READER_BASE_URI } from "./e2e/readerRecords.ts";

// The e2e suite runs the production build (`pnpm build` first) in `vite preview` against a real
// `lae api` and a real `lae crdt-ws` process, which Playwright starts with the development login
// bypass (LAE_DEV_USER) and LAE_ENV=test. The preview server proxies the API paths to the API and
// `/ws/` to crdt-ws (vite.config.ts), so the browser sees one origin as in production; crdt-ws
// allows that origin (LAE_ALLOWED_ORIGINS), and the two processes share LAE_INTERNAL_TOKEN.
//
// The API needs a migrated database: set LAE_DATABASE_URL (`uv run lae migrate` first);
// e2e/globalSetup.ts stores the published records the Public Reader tests open. Ports:
// LAE_E2E_PORT for the preview server (default 4173), LAE_E2E_API_PORT for the API (default
// 8000) and LAE_E2E_CRDT_WS_PORT for crdt-ws (default 8001); agents use their slot's ports
// (docs/agent-workflow.md §3).
const port = Number(process.env.LAE_E2E_PORT ?? "4173");
const apiPort = Number(process.env.LAE_E2E_API_PORT ?? "8000");
const crdtWsPort = Number(process.env.LAE_E2E_CRDT_WS_PORT ?? "8001");
const baseURL = `http://127.0.0.1:${String(port)}`;
const apiURL = `http://127.0.0.1:${String(apiPort)}`;
const crdtWsURL = `http://127.0.0.1:${String(crdtWsPort)}`;
const ci = Boolean(process.env.CI);
/** Shared secret of the api → crdt-ws RPC (`POST /api/records` opens the draft through it). */
const internalToken = "e2e-internal-token";
const devUser = `${E2E_DEV_USER.email}:${E2E_DEV_USER.role}`;

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
        LAE_DEV_USER: devUser,
        LAE_API_HOST: "127.0.0.1",
        LAE_API_PORT: String(apiPort),
        LAE_BASE_URI: READER_BASE_URI,
        LAE_CRDT_WS_URL: crdtWsURL,
        LAE_INTERNAL_TOKEN: internalToken,
      },
      url: `${apiURL}/health/live`,
      reuseExistingServer: !ci,
      stdout: "pipe",
      timeout: 60_000,
    },
    {
      command: "uv run --project ../backend --locked lae crdt-ws",
      env: {
        LAE_ENV: "test",
        LAE_DEV_USER: devUser,
        LAE_CRDT_WS_HOST: "127.0.0.1",
        LAE_CRDT_WS_PORT: String(crdtWsPort),
        LAE_BASE_URI: READER_BASE_URI,
        LAE_INTERNAL_TOKEN: internalToken,
        LAE_ALLOWED_ORIGINS: baseURL,
      },
      url: `${crdtWsURL}/health/live`,
      reuseExistingServer: !ci,
      stdout: "pipe",
      timeout: 60_000,
    },
    {
      command: `corepack pnpm preview --host 127.0.0.1 --port ${String(port)} --strictPort`,
      env: { VITE_API_TARGET: apiURL, VITE_CRDT_WS_TARGET: crdtWsURL },
      url: baseURL,
      reuseExistingServer: !ci,
      timeout: 30_000,
    },
  ],
});
