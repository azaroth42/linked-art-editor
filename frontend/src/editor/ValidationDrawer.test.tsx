import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { stubFetch } from "../api/testing.ts";
import { addNode, setScalar } from "../crdt/paths.ts";
import { draft, renderWidget } from "../widgets/testing.tsx";
import { finding, PROFILE, servedSchema } from "./validation/testing.ts";
import { ValidationDrawer } from "./ValidationDrawer.tsx";

const ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const CLASS = "HumanMadeObject";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderDrawer(doc = draft({ _label: "A vase", "@context": CONTEXT })) {
  const onJump = vi.fn();
  const onAnnounce = vi.fn();
  renderWidget(
    <ValidationDrawer
      recordId={ID}
      recordClass={CLASS}
      doc={doc}
      profile={PROFILE}
      ready
      onJump={onJump}
      onAnnounce={onAnnounce}
    />,
    doc,
  );
  return { doc, onJump, onAnnounce };
}

const CONTEXT = "https://linked.art/ns/v1/linked-art.json";

describe("ValidationDrawer", () => {
  it("checks the schema in the browser after each change and links findings to fields", async () => {
    const user = userEvent.setup();
    stubFetch({
      "GET /api/profiles/schema/HumanMadeObject": () =>
        Response.json(servedSchema("HumanMadeObject")),
    });
    const { doc, onJump } = renderDrawer(draft({ "@context": CONTEXT }));
    const drawer = await screen.findByRole("complementary", { name: "Checks" });
    // The draft has no _label: a finding about the whole record, without a field.
    expect(
      await within(drawer).findByText("Error: Required properties are missing: _label."),
    ).toBeTruthy();
    expect(within(drawer).getByText("This applies to the record as a whole.")).toBeTruthy();

    // A Name whose content is a number: found after the pause, with a way to the Title field.
    act(() => {
      const key = addNode(doc, "identified_by[Name]", { content: "x" });
      setScalar(doc, "identified_by[Name].content", 42);
      expect(key).toBeTruthy();
    });
    const go = await within(drawer).findByRole("button", { name: "Go to Title" });
    expect(
      within(drawer).getByText("Error: This value does not match any of the forms allowed here."),
    ).toBeTruthy();
    await user.click(go);
    expect(onJump).toHaveBeenCalledWith("title");

    await user.click(within(drawer).getByRole("button", { name: /^Hide findings \(2 errors/ }));
    expect(within(drawer).queryByRole("button", { name: "Go to Title" })).toBeNull();
  });

  it("validates on the server, announces the summary, and notices later changes", async () => {
    const user = userEvent.setup();
    const requests = stubFetch({
      "GET /api/profiles/schema/HumanMadeObject": () =>
        Response.json(servedSchema("HumanMadeObject")),
      [`POST /api/records/${ID}/draft/validate`]: () =>
        Response.json({
          ok: false,
          items: [
            finding("LAE-RULE-003", "/identified_by/0/content"),
            finding("LAE-SHACL-002", "/identified_by", "warning"),
          ],
        }),
    });
    const doc = draft({ "@context": CONTEXT, _label: "A vase" });
    act(() => {
      addNode(doc, "identified_by[Name]", { content: "" });
    });
    const { onAnnounce, onJump } = renderDrawer(doc);
    const drawer = await screen.findByRole("complementary", { name: "Checks" });
    expect(
      await within(drawer).findByText("No structural problems. Validate to run every check."),
    ).toBeTruthy();

    await user.click(within(drawer).getByRole("button", { name: "Validate" }));
    await waitFor(() =>
      expect(onAnnounce).toHaveBeenCalledWith("Validation finished: 1 error, 1 warning, no notes."),
    );
    expect(requests.map((request) => request.method)).toContain("POST");
    const items = within(drawer).getAllByRole("listitem");
    expect(items.map((item) => item.dataset.code)).toEqual(["LAE-RULE-003", "LAE-SHACL-002"]);
    expect(
      within(items[0] as HTMLElement).getByText(
        "Error: This text is empty. Enter a value or remove the entry.",
      ),
    ).toBeTruthy();
    await user.click(within(items[1] as HTMLElement).getByRole("button", { name: "Go to Title" }));
    expect(onJump).toHaveBeenCalledWith("title");

    act(() => {
      setScalar(doc, "identified_by[Name].content", "A vase");
    });
    expect(
      await within(drawer).findByText(
        "The draft has changed since it was last validated. Validate again to update these results.",
      ),
    ).toBeTruthy();
  });

  it("says when the server cannot validate", async () => {
    const user = userEvent.setup();
    stubFetch({
      "GET /api/profiles/schema/HumanMadeObject": () =>
        Response.json(servedSchema("HumanMadeObject")),
      [`POST /api/records/${ID}/draft/validate`]: () =>
        Response.json(
          { code: "LAE-VALID-001", path: `/api/records/${ID}/draft/validate`, params: {} },
          { status: 503 },
        ),
    });
    renderDrawer();
    const drawer = await screen.findByRole("complementary", { name: "Checks" });
    await user.click(within(drawer).getByRole("button", { name: "Validate" }));
    expect(
      await within(drawer).findByText("Drafts cannot be read right now. Please try again later."),
    ).toBeTruthy();
  });
});
