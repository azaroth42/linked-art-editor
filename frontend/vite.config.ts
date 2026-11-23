import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";

/**
 * Paths served by the `lae api` process rather than the SPA. The dev server (`pnpm dev`) and the
 * preview server (`pnpm preview`, used by the e2e tests) forward them to `VITE_API_TARGET`, so
 * the browser sees one origin and the session cookie and OIDC redirect work unchanged. The Host
 * header is kept, so `/auth/login` builds its callback URL on the Vite origin. `/reader/` is the
 * Public Reader's script, which `lae api` serves from `dist-reader/` (`pnpm build:reader`).
 */
export const API_PATHS = ["/api/", "/auth/", "/health/", "/id/", "/reader/", "/ws/"] as const;

/** Where the API listens; agents use their slot's port (docs/agent-workflow.md §3). */
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8000";

const proxy: Record<string, ProxyOptions> = Object.fromEntries(
  API_PATHS.map((path) => [path, { target: apiTarget, ws: path === "/ws/" }]),
);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy },
  preview: { proxy },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
