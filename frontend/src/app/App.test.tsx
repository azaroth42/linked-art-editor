import { cleanup, render, screen } from "@testing-library/react";
import { Suspense } from "react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createI18n } from "../i18n/index.ts";
import { stubLocaleFetch } from "../i18n/testing.ts";
import { App } from "./App.tsx";

function renderApp(language: string) {
  render(
    <I18nextProvider i18n={createI18n(language)}>
      <Suspense fallback={null}>
        <App />
      </Suspense>
    </I18nextProvider>,
  );
}

describe("App", () => {
  beforeEach(stubLocaleFetch);
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the translated title inside the main landmark", async () => {
    renderApp("en");
    const heading = await screen.findByRole("heading", { level: 1, name: "Linked Art Editor" });
    expect(screen.getByRole("main").contains(heading)).toBe(true);
    expect(screen.getByRole("main")).toBe(screen.getByTestId("app-root"));
  });

  it("renders the pseudo locale wrapped, accented, and expanded", async () => {
    renderApp("pseudo");
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading.textContent).toMatch(/^\[Ļíñķéð Åŕţ Éðíţóŕ ~+/);
    expect(heading.textContent).toMatch(/\]$/);
  });
});
