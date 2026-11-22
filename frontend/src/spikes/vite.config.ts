/**
 * Spike S5: the app's Vite config plus the spike page as a second entry.
 *
 * `vite --config src/spikes/vite.config.ts` serves the page for the Playwright spike tests, and
 * `vite build --config src/spikes/vite.config.ts` measures what Canvas Panel adds to a bundle.
 * Run both from `frontend/`.
 */
import { mergeConfig } from "vite";

import appConfig from "../../vite.config.ts";

export default mergeConfig(appConfig, {
  // The spike page is not reachable from index.html, so pre-bundle its one dependency up front
  // instead of letting the dev server discover it and reload the page mid-test.
  optimizeDeps: {
    entries: ["index.html", "src/spikes/canvas-panel.html"],
    include: ["@digirati/canvas-panel-web-components"],
  },
  build: {
    rollupOptions: {
      input: { app: "index.html", "spike-s5": "src/spikes/canvas-panel.html" },
    },
  },
});
