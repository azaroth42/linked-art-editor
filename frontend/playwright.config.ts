import { defineConfig, devices } from "@playwright/test";

// Phase 0 serves the production build (`pnpm build` first) with `vite preview`. Phase 1 switches
// the web server to the FastAPI app, which serves the same build. Override the port with
// LAE_E2E_PORT (agents use their slot's API port, docs/agent-workflow.md §3).
const port = Number(process.env.LAE_E2E_PORT ?? "4173");
const baseURL = `http://127.0.0.1:${String(port)}`;
const ci = Boolean(process.env.CI);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: ci,
  retries: 0,
  reporter: ci ? [["github"], ["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `corepack pnpm preview --host 127.0.0.1 --port ${String(port)} --strictPort`,
    url: baseURL,
    reuseExistingServer: !ci,
    timeout: 30_000,
  },
});
