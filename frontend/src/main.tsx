import "./index.css";

import { StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import { createBrowserRouter } from "react-router";

import { createQueryClient } from "./api/queryClient.ts";
import { App } from "./app/App.tsx";
import { routes } from "./app/routes.tsx";
import { createI18n } from "./i18n/index.ts";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("Missing #root element");
}

const i18n = createI18n();
const router = createBrowserRouter(routes);
const queryClient = createQueryClient();

createRoot(container).render(
  <StrictMode>
    <I18nextProvider i18n={i18n}>
      <Suspense fallback={null}>
        <App router={router} queryClient={queryClient} />
      </Suspense>
    </I18nextProvider>
  </StrictMode>,
);
