/**
 * Spike S5 Playwright config. Separate from the app's E2E suite because these tests load public
 * IIIF resources over the network. Run with `corepack pnpm test:spike-s5` from `frontend/`.
 *
 * The dev server uses slot 10's port (agent workflow section 3).
 */
import { defineConfig, devices } from "@playwright/test";

const PORT = 9010;
const PAGE = "/src/spikes/canvas-panel.html";

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  outputDir: "../../test-results/spike-s5",
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    // Playwright's bundled Chromium, headless. This is what CI has.
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    // Branded Google Chrome, headless, only when asked: it carries the H.264/AAC decoders that
    // the open-source Chromium build lacks, so it is the A/V playback check.
    ...(process.env.SPIKE_S5_CHROME
      ? [{ name: "chrome", use: { ...devices["Desktop Chrome"], channel: "chrome" } }]
      : []),
  ],
  webServer: {
    command: `corepack pnpm exec vite --config src/spikes/vite.config.ts --port ${PORT} --strictPort`,
    cwd: "../..",
    url: `http://localhost:${PORT}${PAGE}`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
