import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { type ProxyOptions, searchForWorkspaceRoot } from "vite";
import { defineConfig } from "vitest/config";

/**
 * Paths served by the `lae api` process rather than the SPA. The dev server (`pnpm dev`) and the
 * preview server (`pnpm preview`, used by the e2e tests) forward them to `VITE_API_TARGET`, so
 * the browser sees one origin and the session cookie and OIDC redirect work unchanged. The Host
 * header is kept, so `/auth/login` builds its callback URL on the Vite origin. `/reader/` is the
 * Public Reader's script, which `lae api` serves from `dist-reader/` (`pnpm build:reader`).
 */
export const API_PATHS = ["/api/", "/auth/", "/health/", "/id/", "/reader/"] as const;

/** Where the API listens; agents use their slot's port (docs/agent-workflow.md §3). */
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8000";

/**
 * Draft rooms (`/ws/rooms/{id}`) are served by the `lae crdt-ws` process, not the API (Step 1.8).
 * The browser's `Origin` (the Vite origin) is forwarded unchanged: `crdt-ws` must allow it
 * (`LAE_ALLOWED_ORIGINS`; loopback origins are allowed with `LAE_ENV=development`).
 */
const crdtWsTarget = process.env.VITE_CRDT_WS_TARGET ?? "http://localhost:8001";

const proxy: Record<string, ProxyOptions> = {
  ...Object.fromEntries(API_PATHS.map((path) => [path, { target: apiTarget }])),
  "/ws/": { target: crdtWsTarget, ws: true },
};

/**
 * The CRDT mirror's tests (`src/crdt/*.test.ts`) read the backend's shared projection fixtures
 * and the golden corpus unchanged (never copies that could drift), and the browser schema
 * check's tests read the vendored Linked Art schemas and the server's verdicts on them
 * (`backend/tests/validation/data/schema_browser_cases.json`). Only Vitest's server may serve
 * these directories outside the root.
 */
const TEST_DATA = [
  "../backend/tests/projection/fixtures",
  "../backend/tests/validation/data",
  "../tests/corpus",
  "../profiles/schema",
].map((path) => fileURLToPath(new URL(path, import.meta.url)));
const testFs = process.env.VITEST
  ? { allow: [searchForWorkspaceRoot(process.cwd()), ...TEST_DATA] }
  : undefined;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy, fs: testFs },
  preview: { proxy },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
