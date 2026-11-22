/** Entry point of the Spike S5 page (`canvas-panel.html`); not part of the app bundle. */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { CanvasPanelSpike } from "./CanvasPanelSpike.tsx";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("Missing #root element");
}

createRoot(container).render(
  <StrictMode>
    <CanvasPanelSpike />
  </StrictMode>,
);
