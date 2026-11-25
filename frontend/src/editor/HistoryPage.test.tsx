import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Suspense } from "react";
import { I18nextProvider } from "react-i18next";
import { createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Change, SemanticDiff } from "../api/hooks/publishing.ts";
import type { RevisionSummary } from "../api/hooks/revisions.ts";
import { EDITOR, errorResponse, type Routes, stubFetch } from "../api/testing.ts";
import { App } from "../app/App.tsx";
import { routes } from "../app/routes.tsx";
import { createI18n, DEFAULT_LANGUAGE } from "../i18n/index.ts";
import { RECORD_URI } from "../widgets/testing.tsx";
import { defaultComparison } from "./HistoryPage.tsx";
import { revertedFrom } from "./links.ts";
import { PROFILE } from "./validation/testing.ts";

const ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const NAME = `${RECORD_URI}/node/01937a4e-0000-7000-8000-000000000001`;
const OTHER_USER = "0190a3b2-0000-7000-8000-00000000beef";
const RECORD = `GET /api/records/${ID}`;
const REVISIONS = `GET /api/records/${ID}/revisions`;
const REVERT = (version: number) => `POST /api/records/${ID}/revisions/${String(version)}/revert`;
const DIFF_PATH = new RegExp(`^/api/records/${ID}/revisions/(\\d+)/diff/(\\d+)$`);

const REVISION_LIST: RevisionSummary[] = [
  {
    version: 1,
    created_at: "2026-11-20T10:00:00Z",
    authors: [OTHER_USER],
    published_by: null,
    summary: "",
  },
  {
    version: 2,
    created_at: "2026-11-21T10:00:00Z",
    authors: [OTHER_USER],
    published_by: OTHER_USER,
    summary: "Add a material",
  },
  {
    version: 3,
    created_at: "2026-11-22T10:00:00Z",
    authors: [EDITOR.id],
    published_by: EDITOR.id,
    summary: "Give the vase its title",
  },
];

const TITLES: Record<number, string> = { 1: "Untitled", 2: "Untitled", 3: "A Fine Vase" };

/** The title change between two versions, as `lae.diff.semantic` reports it. */
function titleChange(from: number, to: number): Change[] {
  if (TITLES[from] === TITLES[to]) {
    return [];
  }
  return [
    {
      kind: "update_literal",
      path: "identified_by[Name].content",
      parent: [
        {
          property: "identified_by",
          key: NAME,
          id: NAME,
          type: "Name",
          classified_as: null,
          label: null,
        },
      ],
      property: "content",
      op: "change",
      key: null,
      before: null,
      old: TITLES[from],
      new: TITLES[to],
      text_edit: null,
    },
  ];
}

function api(extra: Routes = {}): Routes {
  return {
    "GET /auth/me": () => Response.json(EDITOR),
    [RECORD]: () =>
      Response.json(
        { id: RECORD_URI, type: "HumanMadeObject" },
        { headers: { "X-Draft-Epoch": "8" } },
      ),
    [REVISIONS]: () => Response.json(REVISION_LIST),
    "GET /api/profiles/HumanMadeObject": () => Response.json(PROFILE),
    ...extra,
  };
}

/** Answers `GET …/revisions/{m}/diff/{n}` (stubFetch matches exact paths only). */
function withDiffs(requests: Request[]) {
  const fetch = globalThis.fetch;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request =
      input instanceof Request
        ? input
        : new Request(new URL(String(input), globalThis.location.origin), init);
    const match = DIFF_PATH.exec(new URL(request.url).pathname);
    if (match === null) {
      return fetch(input, init);
    }
    requests.push(request);
    const from = Number(match[1]);
    const to = Number(match[2]);
    const diff: SemanticDiff = {
      record_id: ID,
      from_version: from,
      to_version: to,
      draft_epoch: null,
      changes: titleChange(from, to),
    };
    return Response.json(diff);
  });
}

function renderHistory(routesApi: Routes, user = EDITOR) {
  const requests = stubFetch({ ...routesApi, "GET /auth/me": () => Response.json(user) });
  const diffs: Request[] = [];
  withDiffs(diffs);
  const router = createMemoryRouter(routes, { initialEntries: [`/records/${ID}/history`] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <I18nextProvider i18n={createI18n(DEFAULT_LANGUAGE)}>
      <Suspense fallback={null}>
        <App router={router} queryClient={queryClient} />
      </Suspense>
    </I18nextProvider>,
  );
  return { router, requests, diffs };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("defaultComparison", () => {
  it("compares the latest version with the one before it", () => {
    expect(defaultComparison(REVISION_LIST)).toEqual({ from: 2, to: 3 });
    expect(defaultComparison(REVISION_LIST.slice(0, 1))).toEqual({ from: 1, to: 1 });
    expect(defaultComparison([])).toBeNull();
  });
});

describe("revertedFrom", () => {
  it("reads only a well-formed revert state", () => {
    expect(revertedFrom({ reverted: { version: 2, draftEpoch: 7 } })).toEqual({
      version: 2,
      draftEpoch: 7,
    });
    expect(revertedFrom(null)).toBeNull();
    expect(revertedFrom({ reverted: { version: "2", draftEpoch: 7 } })).toBeNull();
    expect(revertedFrom({ other: true })).toBeNull();
  });
});

describe("HistoryPage", () => {
  it("lists the versions newest first, with their publisher, summary and public page", async () => {
    renderHistory(api());
    const table = await screen.findByRole("table", { name: "3 published versions" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.getAttribute("data-version"))).toEqual(["3", "2", "1"]);
    const [latest, middle, first] = rows as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(latest).getByRole("rowheader").textContent).toBe("Version 3 (current)");
    expect(latest.textContent).toContain("You");
    expect(latest.textContent).toContain("Give the vase its title");
    expect(middle.textContent).toContain("Another user");
    expect(first.textContent).toContain("Bulk import");
    expect(first.textContent).toContain("No summary");
    const time = latest.querySelector("time");
    expect(time?.getAttribute("dateTime")).toBe("2026-11-22T10:00:00Z");
    const view = within(first).getByRole("link", {
      name: "View version 1 (opens in a new tab)",
    });
    expect(view.getAttribute("href")).toBe(`/id/${ID}?version=1`);
    expect(view.getAttribute("target")).toBe("_blank");
  });

  it("compares the latest two versions, then any two in either direction", async () => {
    const user = userEvent.setup();
    const { diffs } = renderHistory(api());
    const section = await screen.findByRole("region", {
      name: "Changes from version 2 to version 3",
    });
    await within(section).findByText("Title: changed from “Untitled” to “A Fine Vase”");
    expect(within(section).getByText("1 change")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "Compare from version 2" })).toHaveProperty(
      "checked",
      true,
    );
    expect(screen.getByRole("radio", { name: "Compare to version 3" })).toHaveProperty(
      "checked",
      true,
    );

    // The other direction.
    await user.click(within(section).getByRole("button", { name: "Swap the versions" }));
    const swapped = await screen.findByRole("region", {
      name: "Changes from version 3 to version 2",
    });
    await within(swapped).findByText("Title: changed from “A Fine Vase” to “Untitled”");

    // Any two: 1 → 2 has no changes; the same version twice is not compared at all.
    await user.click(screen.getByRole("radio", { name: "Compare from version 1" }));
    const oneTwo = await screen.findByRole("region", {
      name: "Changes from version 1 to version 2",
    });
    await within(oneTwo).findByText("The two versions are the same.");
    expect(within(oneTwo).queryByTestId("change-list")).toBeNull();
    const fetched = diffs.length;
    await user.click(screen.getByRole("radio", { name: "Compare to version 1" }));
    await screen.findByText("Choose two different versions to compare.");
    expect(screen.getByRole("button", { name: "Swap the versions" }).hasAttribute("disabled")).toBe(
      true,
    );
    expect(diffs.length).toBe(fetched);
    expect(diffs.map((request) => new URL(request.url).pathname)).toEqual([
      `/api/records/${ID}/revisions/2/diff/3`,
      `/api/records/${ID}/revisions/3/diff/2`,
      `/api/records/${ID}/revisions/1/diff/2`,
    ]);
  });

  it("reverts after confirmation and opens the editor on the new draft", async () => {
    const user = userEvent.setup();
    const reverted = { id: ID, version: 3, draft_epoch: 9, base_version: 3 };
    const { router, requests } = renderHistory(api({ [REVERT(1)]: () => Response.json(reverted) }));
    const revert = await screen.findByRole("button", { name: "Revert to version 1" });
    expect(
      screen.getByText(
        "Reverting puts an earlier version into the draft. Nothing is published until you publish the draft.",
      ),
    ).toBeTruthy();

    // Cancel first: nothing is sent, focus goes back to the button.
    await user.click(revert);
    let dialog = screen.getByRole("dialog", { name: "Revert to version 1?" });
    expect(dialog.textContent).toContain(
      "Nothing is published: the public record stays as it is until you publish the draft.",
    );
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBe(document.activeElement);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(revert).toBe(document.activeElement);
    expect(requests.filter((request) => request.method === "POST")).toEqual([]);

    await user.click(revert);
    dialog = screen.getByRole("dialog", { name: "Revert to version 1?" });
    await user.click(within(dialog).getByRole("button", { name: "Revert" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/edit/${ID}`));
    expect(revertedFrom(router.state.location.state)).toEqual({ version: 1, draftEpoch: 9 });
    expect(
      requests.filter((request) => request.method === "POST").map((r) => new URL(r.url).pathname),
    ).toEqual([`/api/records/${ID}/revisions/1/revert`]);
    // The revert may only replace the draft this page knew about (issue #178).
    const sent = requests.find((request) => request.method === "POST");
    expect(await sent?.json()).toEqual({ draft_epoch: 8 });
  });

  it("reads the record again after a refused revert, so a retry names the current epoch", async () => {
    const user = userEvent.setup();
    let epoch = 8;
    const bodies: unknown[] = [];
    const { router, requests } = renderHistory(
      api({
        [RECORD]: () =>
          Response.json(
            { id: RECORD_URI, type: "HumanMadeObject" },
            { headers: { "X-Draft-Epoch": String(epoch) } },
          ),
        [REVERT(2)]: async (request: Request) => {
          const body = (await request.json()) as { draft_epoch: number };
          bodies.push(body);
          if (bodies.length === 1) {
            epoch = 9; // the draft the page knew about was published on the way
          }
          // As the server: only a request that names the record's epoch is carried out.
          return body.draft_epoch === epoch
            ? Response.json({ id: ID, version: 2, draft_epoch: 10, base_version: 4 })
            : errorResponse(409, "LAE-CRDT-007", `/api/records/${ID}/revisions/2/revert`);
        },
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Revert to version 2" }));
    const dialog = screen.getByRole("dialog", { name: "Revert to version 2?" });
    await user.click(within(dialog).getByRole("button", { name: "Revert" }));
    await within(dialog).findByText(
      "The draft was published or discarded in the meantime. Reload to continue.",
    );
    expect(bodies).toEqual([{ draft_epoch: 8 }]);
    expect(router.state.location.pathname).toBe(`/records/${ID}/history`);
    // The record is read again without a reload of the page, and the next attempt goes through.
    const reads = () => requests.filter((r) => new URL(r.url).pathname === `/api/records/${ID}`);
    await waitFor(() => expect(reads().length).toBe(2));
    await waitFor(async () => {
      if (router.state.location.pathname !== `/edit/${ID}`) {
        await user.click(within(dialog).getByRole("button", { name: "Revert" }));
      }
      expect(router.state.location.pathname).toBe(`/edit/${ID}`);
    });
    expect(bodies.at(-1)).toEqual({ draft_epoch: 9 });
  });

  it("explains a failed revert and stays on the page", async () => {
    const user = userEvent.setup();
    const { router } = renderHistory(
      api({
        [REVERT(2)]: () =>
          errorResponse(409, "LAE-CRDT-005", `/api/records/${ID}/revisions/2/revert`),
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Revert to version 2" }));
    const dialog = screen.getByRole("dialog", { name: "Revert to version 2?" });
    await user.click(within(dialog).getByRole("button", { name: "Revert" }));
    await within(dialog).findByText("This record has been deleted or merged and cannot be edited.");
    expect(router.state.location.pathname).toBe(`/records/${ID}/history`);
  });

  it("offers no revert without the publish permission", async () => {
    renderHistory(api(), { ...EDITOR, permissions: ["edit_draft"] });
    await screen.findByRole("table");
    expect(screen.queryByRole("button", { name: /^Revert/ })).toBeNull();
    expect(screen.queryByText(/^Reverting puts/)).toBeNull();
  });

  it("says when the record has not been published, or does not exist", async () => {
    renderHistory(api({ [REVISIONS]: () => Response.json([]) }));
    await screen.findByText("This record has not been published yet.");
    expect(screen.queryByRole("table")).toBeNull();
    cleanup();
    vi.unstubAllGlobals();

    const missing = () => errorResponse(404, "LAE-REC-001", `/api/records/${ID}`);
    renderHistory(api({ [RECORD]: missing, [REVISIONS]: missing }));
    await screen.findByText("No such record.");
    expect(screen.getByRole("heading", { level: 1, name: "Revision history" })).toBeTruthy();
  });
});
