import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Suspense } from "react";
import { I18nextProvider } from "react-i18next";
import { createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { anonymous, EDITOR, errorResponse, type Routes, stubFetch } from "../api/testing.ts";
import { createI18n } from "../i18n/index.ts";
import { App } from "./App.tsx";
import { routes } from "./routes.tsx";

const RECORD_ID = "0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6c";

function renderApp(path: string, api: Routes, language = "en") {
  const requests = stubFetch(api);
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <I18nextProvider i18n={createI18n(language)}>
      <Suspense fallback={null}>
        <App router={router} queryClient={queryClient} />
      </Suspense>
    </I18nextProvider>,
  );
  return { router, requests };
}

const signedIn: Routes = { "GET /auth/me": () => Response.json(EDITOR) };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("shows the home page and the signed-in user", async () => {
    renderApp("/", signedIn);
    const heading = await screen.findByRole("heading", { level: 1, name: "Linked Art Editor" });
    expect(screen.getByRole("main").contains(heading)).toBe(true);
    const header = screen.getByRole("banner");
    expect(within(header).getByText("Signed in as editor@example.org (Editor)")).toBeDefined();
    expect(within(header).getByRole("button", { name: "Sign out" })).toBeDefined();
    // The title is set in an effect after render; wait for it instead of reading it once.
    await waitFor(() => expect(document.title).toBe("Linked Art Editor"));
  });

  it("sends an anonymous visitor to the login page, which returns to the requested page", async () => {
    const { router } = renderApp(`/edit/${RECORD_ID}?tab=names`, { "GET /auth/me": anonymous });
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(router.state.location.pathname).toBe("/login");
    const next = `/edit/${RECORD_ID}?tab=names`;
    expect(router.state.location.search).toBe(`?${new URLSearchParams({ next }).toString()}`);
    const link = screen.getByRole("link", { name: "Continue to sign-in" });
    expect(link.getAttribute("href")).toBe(
      `/auth/login?${new URLSearchParams({ next }).toString()}`,
    );
    // The title is set in an effect after render; wait for it instead of reading it once.
    await waitFor(() => expect(document.title).toBe("Sign in – Linked Art Editor"));
  });

  it("sends a signed-in user from the login page to next", async () => {
    const { router } = renderApp(`/login?next=/edit/${RECORD_ID}`, signedIn);
    await screen.findByRole("heading", { level: 1, name: "Edit record" });
    expect(router.state.location.pathname).toBe(`/edit/${RECORD_ID}`);
    expect(screen.getByText(`Record ${RECORD_ID}`)).toBeDefined();
  });

  it("ignores an off-site next", async () => {
    const { router } = renderApp("/login?next=//evil.example/", signedIn);
    await screen.findByRole("heading", { level: 1, name: "Linked Art Editor" });
    expect(router.state.location.pathname).toBe("/");
  });

  it("links the editor and the revision history", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(`/records/${RECORD_ID}/history`, signedIn);
    await screen.findByRole("heading", { level: 1, name: "Revision history" });
    // The title is set in an effect after render; wait for it instead of reading it once.
    await waitFor(() => expect(document.title).toBe("Revision history – Linked Art Editor"));
    await user.click(screen.getByRole("link", { name: "Back to the editor" }));
    await screen.findByRole("heading", { level: 1, name: "Edit record" });
    expect(router.state.location.pathname).toBe(`/edit/${RECORD_ID}`);
    await user.click(screen.getByRole("link", { name: "Revision history" }));
    await screen.findByRole("heading", { level: 1, name: "Revision history" });
  });

  it("shows a 404 page for unknown paths, signed in or not", async () => {
    renderApp("/no/such/page", { "GET /auth/me": anonymous });
    await screen.findByRole("heading", { level: 1, name: "Page not found" });
    expect(screen.getByText("There is no page at /no/such/page.")).toBeDefined();
    expect(screen.getByRole("link", { name: "Go to the home page" }).getAttribute("href")).toBe(
      "/",
    );
    // The 404 page does not wait for /auth/me; the header offers sign-in once it has answered.
    await within(screen.getByRole("banner")).findByRole("link", { name: "Sign in" });
  });

  it("offers a retry when the API cannot be reached", async () => {
    const user = userEvent.setup();
    let up = false;
    renderApp("/", {
      "GET /auth/me": () =>
        up ? Response.json(EDITOR) : errorResponse(503, "LAE-HTTP-503", "/auth/me"),
    });
    await screen.findByText("The server cannot be reached right now.");
    up = true;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("heading", { level: 1, name: "Linked Art Editor" });
  });

  it("signs out and returns to the login page", async () => {
    const user = userEvent.setup();
    let session = true;
    const { router, requests } = renderApp(`/edit/${RECORD_ID}`, {
      "GET /auth/me": (request) => (session ? Response.json(EDITOR) : anonymous(request)),
      "POST /auth/logout": () => {
        session = false;
        return new Response(null, { status: 204 });
      },
    });
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(requests.map((r) => `${r.method} ${new URL(r.url).pathname}`)).toContain(
      "POST /auth/logout",
    );
    expect(router.state.location.search).toBe(
      `?${new URLSearchParams({ next: `/edit/${RECORD_ID}` }).toString()}`,
    );
  });

  it("renders the pseudo locale wrapped, accented, and expanded", async () => {
    renderApp("/", signedIn, "pseudo");
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading.textContent).toMatch(/^\[Ļíñķéð Åŕţ Éðíţóŕ ~+/);
    expect(heading.textContent).toMatch(/\]$/);
  });
});
