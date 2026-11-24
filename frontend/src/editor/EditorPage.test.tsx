/**
 * The editor page derives the draft's root `_label` with the label language the server sent
 * with the record (`X-Label-Language`, Step 1.19a, contract §5 test 10b; invariant 15). The live
 * draft (`useRecordDoc`) is replaced by a local document: no WebSocket.
 */
import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Suspense } from "react";
import { I18nextProvider } from "react-i18next";
import { createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as Y from "yjs";

import { EDITOR, stubFetch } from "../api/testing.ts";
import { App } from "../app/App.tsx";
import { routes } from "../app/routes.tsx";
import { hydrate } from "../crdt/hydrate.ts";
import { project } from "../crdt/project.ts";
import type { JsonObject } from "../crdt/rules.ts";
import type { RecordDoc } from "../crdt/useRecordDoc.ts";
import { createI18n, DEFAULT_LANGUAGE } from "../i18n/index.ts";
import { ENGLISH, FRENCH, PRIMARY_NAME, RECORD_URI } from "../widgets/testing.tsx";
import { PROFILE, servedSchema } from "./validation/testing.ts";

const mocked = vi.hoisted(() => ({
  live: null as RecordDoc | null,
  checked: [] as JsonObject[],
}));

vi.mock("../crdt/useRecordDoc.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../crdt/useRecordDoc.ts")>()),
  useRecordDoc: () => mocked.live,
}));

vi.mock("./validation/schemaCheck.ts", async (importOriginal) => {
  const original = await importOriginal<typeof import("./validation/schemaCheck.ts")>();
  return {
    ...original,
    checkSchema: (...args: Parameters<typeof original.checkSchema>) => {
      mocked.checked.push(args[1]);
      return original.checkSchema(...args);
    },
  };
});

const ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const CONTEXT = "https://linked.art/ns/v1/linked-art.json";

/** A record with an English and a French primary name, English first. */
const RECORD = {
  "@context": CONTEXT,
  id: RECORD_URI,
  type: "HumanMadeObject",
  _label: "Stored label",
  identified_by: [
    {
      type: "Name",
      content: "The Night Watch",
      classified_as: [{ id: PRIMARY_NAME, type: "Type" }],
      language: [{ id: ENGLISH, type: "Language" }],
    },
    {
      type: "Name",
      content: "La Ronde de nuit",
      classified_as: [{ id: PRIMARY_NAME, type: "Type" }],
      language: [{ id: FRENCH, type: "Language" }],
    },
  ],
};

function liveDraft(doc: Y.Doc): RecordDoc {
  return {
    doc,
    provider: null,
    session: { doc, localOrigin: {} } as unknown as RecordDoc["session"],
    undo: () => undefined,
    redo: () => undefined,
    connected: true,
    synced: true,
    closeCode: null,
    peers: [],
    readOnly: false,
    canUndo: false,
    canRedo: false,
  };
}

function renderEditor(labelLanguage: string) {
  const doc = hydrate(RECORD);
  mocked.live = liveDraft(doc);
  mocked.checked = [];
  stubFetch({
    "GET /auth/me": () => Response.json(EDITOR),
    [`GET /api/records/${ID}`]: () =>
      Response.json(project(doc), {
        headers: { "X-Draft-Epoch": "3", "X-Label-Language": labelLanguage },
      }),
    "GET /api/profiles/HumanMadeObject": () => Response.json(PROFILE),
    "GET /api/profiles/schema/HumanMadeObject": () =>
      Response.json(servedSchema("HumanMadeObject")),
  });
  const router = createMemoryRouter(routes, { initialEntries: [`/edit/${ID}`] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <I18nextProvider i18n={createI18n(DEFAULT_LANGUAGE)}>
      <Suspense fallback={null}>
        <App router={router} queryClient={queryClient} />
      </Suspense>
    </I18nextProvider>,
  );
  return doc;
}

async function rawLabel(): Promise<unknown> {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Show JSON-LD" }));
  const raw = await screen.findByTestId("raw-jsonld");
  return (JSON.parse(raw.textContent) as JsonObject)._label;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  mocked.live = null;
});

describe("EditorPage", () => {
  it("the editor derives with the label language of the record it loaded", async () => {
    const doc = renderEditor("fr");
    expect(await rawLabel()).toBe("La Ronde de nuit");
    // The drawer's live check got the same derived document.
    const drawer = await screen.findByRole("complementary", { name: "Checks" });
    await within(drawer).findByText("No structural problems. Validate to run every check.");
    await waitFor(() => expect(mocked.checked.length).toBeGreaterThan(0));
    expect(mocked.checked.every((document) => document._label === "La Ronde de nuit")).toBe(true);
    // Nothing was written into the draft for it.
    expect(project(doc)._label).toBe("Stored label");
  });

  it("derives in English when the server says en", async () => {
    renderEditor("en");
    expect(await rawLabel()).toBe("The Night Watch");
    await waitFor(() => expect(mocked.checked.length).toBeGreaterThan(0));
    expect(mocked.checked.every((document) => document._label === "The Night Watch")).toBe(true);
  });
});
