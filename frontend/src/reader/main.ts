/**
 * The Public Reader's only script (Step 1.21, decision D8): no React, no SPA code.
 *
 * The server-rendered page at `/id/{uuid}` includes it only when the record has a IIIF resource,
 * as `/reader/assets/reader.js` (built by `corepack pnpm build:reader`, served by `lae api`). It
 * imports Canvas Panel (about 175 kB gzip, docs/spikes/S5-canvas-panel.md section 6) only when the
 * page has a `<canvas-panel>`, waits for the element to be defined, and sets each panel up.
 */
import { globalVault, type CanvasPanelElement } from "./canvasPanel.ts";
import { setUpViewer } from "./viewer.ts";

async function main(): Promise<void> {
  const panels = [...document.querySelectorAll<CanvasPanelElement>("canvas-panel")];
  if (panels.length === 0) {
    return;
  }
  await import("@digirati/canvas-panel-web-components");
  await customElements.whenDefined("canvas-panel");
  const vault = globalVault();
  if (vault === undefined) {
    return;
  }
  await Promise.all(panels.map((panel) => setUpViewer(panel, vault)));
}

void main();
