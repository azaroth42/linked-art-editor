import { StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import { App } from "./app/App.tsx";
import { createI18n } from "./i18n/index.ts";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("Missing #root element");
}

const i18n = createI18n();

createRoot(container).render(
  <StrictMode>
    <I18nextProvider i18n={i18n}>
      <Suspense fallback={null}>
        <App />
      </Suspense>
    </I18nextProvider>
  </StrictMode>,
);
