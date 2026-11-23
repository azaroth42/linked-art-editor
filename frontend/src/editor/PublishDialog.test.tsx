import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Change, SemanticDiff } from "../api/hooks/publishing.ts";
import { errorResponse, type Handler, stubFetch } from "../api/testing.ts";
import { draft, RECORD_URI, renderWidget } from "../widgets/testing.tsx";
import { PublishDialog } from "./PublishDialog.tsx";
import { finding, PROFILE } from "./validation/testing.ts";

const ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const STUB = "0192f0c1-7b1e-7c3a-9d2e-00000000e5b1";
const NAME = `${RECORD_URI}/node/01937a4e-0000-7000-8000-000000000001`;
const NEW_NODE = `${RECORD_URI}/node/01937a4e-0000-7000-8000-000000000002`;
const PUBLISH = `POST /api/records/${ID}/publish`;
const VALIDATE = `POST /api/records/${ID}/draft/validate`;
const DIFF = `POST /api/records/${ID}/draft/diff`;

const nameStep = {
  property: "identified_by",
  key: NAME,
  id: NAME,
  type: "Name",
  classified_as: null,
  label: null,
};

const CHANGES: Change[] = [
  {
    kind: "update_literal",
    path: `identified_by[Name].content`,
    parent: [nameStep],
    property: "content",
    op: "change",
    key: null,
    before: null,
    old: "Untitled",
    new: "A Fine Vase",
    text_edit: null,
  },
  {
    kind: "add_ref",
    path: "made_of",
    parent: [],
    property: "made_of",
    key: "http://vocab.getty.edu/aat/300010662",
    before: null,
    uri: "http://vocab.getty.edu/aat/300010662",
    type: "Material",
    label: "porcelain",
    value: {},
  },
  {
    // An empty nested node added on its own arrives as a reference to its node IRI (1.14 §8).
    kind: "add_ref",
    path: "dimension",
    parent: [],
    property: "dimension",
    key: NEW_NODE,
    before: null,
    uri: NEW_NODE,
    type: "Dimension",
    label: null,
    value: { id: NEW_NODE, type: "Dimension" },
  },
  {
    kind: "remove_node",
    path: "identified_by",
    parent: [],
    property: "identified_by",
    key: "x",
    node_type: "Identifier",
    node_id: "x",
    summary_fields: { content: "1990.1" },
    value: {},
  },
  {
    kind: "reorder",
    path: "referred_to_by",
    parent: [],
    property: "referred_to_by",
    order: ["b", "a"],
    previous: ["a", "b"],
  },
];

function diff(changes: Change[] = CHANGES): SemanticDiff {
  return { record_id: ID, from_version: 1, to_version: null, draft_epoch: 3, changes };
}

/** The draft as the dialog's findings were made in. */
const SNAPSHOT = {
  id: RECORD_URI,
  type: "HumanMadeObject",
  identified_by: [{ id: NAME, type: "Name", content: "" }],
};

const PUBLISHED = { version: 2, published: [ID], draft_epoch: 4 };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderDialog(routes: Record<string, Handler>) {
  const requests = stubFetch({
    [DIFF]: () => Response.json(diff()),
    ...routes,
  });
  const props = { onClose: vi.fn(), onPublished: vi.fn(), onJump: vi.fn() };
  const doc = draft({ identified_by: [{ id: NAME, type: "Name", content: "" }] });
  renderWidget(
    <PublishDialog
      recordId={ID}
      recordUri={RECORD_URI}
      profile={PROFILE}
      snapshot={() => SNAPSHOT}
      {...props}
    />,
    doc,
  );
  return { requests, ...props };
}

async function bodyOf(requests: Request[], route: string): Promise<unknown[]> {
  const [method, path] = route.split(" ");
  const matching = requests.filter(
    (request) => request.method === method && new URL(request.url).pathname === path,
  );
  return Promise.all(matching.map((request) => request.clone().json() as Promise<unknown>));
}

describe("PublishDialog", () => {
  it("shows the changes, asks for every warning, and publishes with the accepted ones", async () => {
    const user = userEvent.setup();
    const { requests, onPublished } = renderDialog({
      [VALIDATE]: () =>
        Response.json({ ok: true, items: [finding("LAE-SHACL-002", "/identified_by", "warning")] }),
      [PUBLISH]: () => Response.json(PUBLISHED),
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    const changes = await within(dialog).findByTestId("change-list");
    expect(
      within(changes)
        .getAllByRole("listitem")
        .map((item) => item.firstChild?.textContent),
    ).toEqual([
      "Title: changed from “Untitled” to “A Fine Vase”",
      "Materials: added porcelain (Material)",
      "Dimensions: added Dimension",
      "Identifiers: removed Identifier “1990.1”",
      "referred_to_by: changed the order",
    ]);
    expect(
      within(dialog).getByRole("heading", { name: "5 changes since the published version" }),
    ).toBeTruthy();

    const publish = within(dialog).getByRole("button", { name: "Publish" });
    expect((publish as HTMLButtonElement).disabled).toBe(true);
    const warning = within(dialog).getByRole("checkbox", {
      name: "Publish anyway: One of the object's Names should be classified as Primary Name.",
    });
    await user.click(warning);
    await user.type(within(dialog).getByLabelText("Summary of changes (optional)"), "Retitled");
    expect((publish as HTMLButtonElement).disabled).toBe(false);
    await user.click(publish);

    await waitFor(() => expect(onPublished).toHaveBeenCalledWith(PUBLISHED));
    expect(await bodyOf(requests, PUBLISH)).toEqual([
      {
        summary: "Retitled",
        accepted_warnings: [{ code: "LAE-SHACL-002", path: "/identified_by" }],
      },
    ]);
  });

  it("blocks on violations and leads to their fields", async () => {
    const user = userEvent.setup();
    const { onJump, requests } = renderDialog({
      [VALIDATE]: () =>
        Response.json({ ok: false, items: [finding("LAE-RULE-003", "/identified_by/0/content")] }),
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    const errors = await within(dialog).findByRole("region", { name: "Errors to fix" });
    expect(
      within(errors).getByText("Error: This text is empty. Enter a value or remove the entry."),
    ).toBeTruthy();
    expect(
      (within(dialog).getByRole("button", { name: "Publish" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    await user.click(within(errors).getByRole("button", { name: "Go to Title" }));
    expect(onJump).toHaveBeenCalledWith("title");
    expect(await bodyOf(requests, PUBLISH)).toEqual([]);
  });

  it("adds the warnings of a bundled new record that publish reports, for that record", async () => {
    const user = userEvent.setup();
    let calls = 0;
    const stubWarning = finding("LAE-SHACL-020", "/identified_by", "warning");
    const { requests, onPublished } = renderDialog({
      [VALIDATE]: () => Response.json({ ok: true, items: [] }),
      [PUBLISH]: () => {
        calls += 1;
        return calls === 1
          ? errorResponse(409, "LAE-PUB-002", `/api/records/${ID}/publish`, {
              records: { [STUB]: [stubWarning] },
              unaccepted: [{ record_id: STUB, code: "LAE-SHACL-020", path: "/identified_by" }],
            })
          : Response.json(PUBLISHED);
      },
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    await within(dialog).findByTestId("change-list");
    await user.click(within(dialog).getByRole("button", { name: "Publish" }));
    expect(
      await within(dialog).findByText(
        "This draft has warnings. Review them and confirm to publish anyway.",
      ),
    ).toBeTruthy();
    const publish = within(dialog).getByRole("button", { name: "Publish" }) as HTMLButtonElement;
    expect(publish.disabled).toBe(true);
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: `Publish anyway (new record ${STUB}): A concept should have at least one Name in identified_by.`,
      }),
    );
    await user.click(publish);
    await waitFor(() => expect(onPublished).toHaveBeenCalledWith(PUBLISHED));
    const bodies = await bodyOf(requests, PUBLISH);
    expect(bodies[1]).toEqual({
      summary: "",
      accepted_warnings: [{ code: "LAE-SHACL-020", path: "/identified_by", record_id: STUB }],
    });
  });

  it("offers to check again when the draft changed during the publish (conflict)", async () => {
    const user = userEvent.setup();
    const { requests } = renderDialog({
      [VALIDATE]: () => Response.json({ ok: true, items: [] }),
      [PUBLISH]: () => errorResponse(409, "LAE-PUB-004", `/api/records/${ID}/publish`, { id: ID }),
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    await within(dialog).findByTestId("change-list");
    await user.click(within(dialog).getByRole("button", { name: "Publish" }));
    expect(
      await within(dialog).findByText(
        "The draft changed while it was being published. Nothing was published; check the draft and publish again.",
      ),
    ).toBeTruthy();
    const publish = within(dialog).getByRole("button", { name: "Publish" }) as HTMLButtonElement;
    expect(publish.disabled).toBe(true);
    await user.click(within(dialog).getByRole("button", { name: "Check the draft again" }));
    await waitFor(() => expect(publish.disabled).toBe(false));
    const paths = requests.map((request) => `${request.method} ${new URL(request.url).pathname}`);
    expect(paths.filter((path) => path === VALIDATE)).toHaveLength(2);
    expect(paths.filter((path) => path === DIFF)).toHaveLength(2);
  });

  it("explains a draft based on an older version (stale) and other refusals", async () => {
    const user = userEvent.setup();
    renderDialog({
      [VALIDATE]: () => Response.json({ ok: true, items: [] }),
      [PUBLISH]: () =>
        errorResponse(409, "LAE-PUB-006", `/api/records/${ID}/publish`, {
          id: ID,
          base_version: 1,
          version: 2,
        }),
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    await within(dialog).findByTestId("change-list");
    await user.click(within(dialog).getByRole("button", { name: "Publish" }));
    expect(
      await within(dialog).findByText(
        "This draft was started from version 1, but the record is now at version 2. Discard the draft and make the changes again.",
      ),
    ).toBeTruthy();
  });

  it("says when the draft cannot be checked", async () => {
    renderDialog({
      [VALIDATE]: () =>
        errorResponse(404, "LAE-VALID-002", `/api/records/${ID}/draft/validate`, { id: ID }),
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    expect(await within(dialog).findByText("This record has no draft to validate.")).toBeTruthy();
    expect(
      (within(dialog).getByRole("button", { name: "Publish" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("says when nothing has changed", async () => {
    stubFetch({});
    renderDialog({
      [VALIDATE]: () => Response.json({ ok: true, items: [] }),
      [DIFF]: () => Response.json(diff([])),
    });
    const dialog = await screen.findByRole("dialog", { name: "Publish this record" });
    expect(
      await within(dialog).findByText("Nothing has changed since the published version."),
    ).toBeTruthy();
  });
});
