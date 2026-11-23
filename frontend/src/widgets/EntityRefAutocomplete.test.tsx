import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { stubFetch } from "../api/testing.ts";
import { EntityRefAutocomplete } from "./EntityRefAutocomplete.tsx";
import { draft, projected, renderWidget, slot } from "./testing.tsx";

const CREATOR = slot({
  id: "creator",
  label: { en: "Creator" },
  path: "produced_by.carried_out_by",
  widget: "entity_ref",
  allowed_classes: ["Person", "Group"],
});

const LEONARDO = "http://vocab.getty.edu/ulan/500010879";
const DOURIS = "https://data.test/id/01937a4e-0000-7000-8000-00000000e2e2";
const DOURIS_GROUP = "https://data.test/id/01937a4e-0000-7000-8000-00000000e2e9";
const CREATED = "https://data.test/id/01937a4e-0000-7000-8000-00000000e2ea";

function leonardo() {
  return draft({
    produced_by: {
      type: "Production",
      carried_out_by: [{ id: LEONARDO, type: "Person" }],
    },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("EntityRefAutocomplete", () => {
  it("lists references with their transcluded labels and removes one", async () => {
    const user = userEvent.setup();
    stubFetch({});
    const doc = leonardo();
    renderWidget(<EntityRefAutocomplete slot={CREATOR} base={null} />, doc, {
      refLabels: new Map([[LEONARDO, "Leonardo da Vinci"]]),
    });
    expect(await screen.findByText("Leonardo da Vinci")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Remove Leonardo da Vinci" }));
    expect(projected(doc).produced_by).toEqual({ type: "Production", id: "<node>" });
  });

  it("searches the slot's classes, shows context, and inserts the picked reference", async () => {
    const user = userEvent.setup();
    const requests = stubFetch({
      "GET /api/search/autocomplete": () =>
        Response.json([
          { id: DOURIS, type: "Person", label: "Douris", context: "Athens", source: "index" },
          {
            id: DOURIS_GROUP,
            type: "Group",
            label: "Douris workshop",
            context: null,
            source: "overlay",
          },
        ]),
    });
    const doc = leonardo();
    renderWidget(<EntityRefAutocomplete slot={CREATOR} base={null} />, doc);
    const input = await screen.findByRole("combobox", { name: "Search to add to Creator" });
    await user.type(input, "Dou");
    const listbox = await screen.findByRole("listbox", { name: "Suggestions for Creator" });
    const option = await within(listbox).findByRole("option", { name: /Douris\s*Person: Athens/ });
    expect(within(listbox).getByRole("option", { name: /Douris workshop\s*Group/ })).toBeTruthy();
    expect(within(listbox).getByRole("option", { name: "Create new Person “Dou”…" })).toBeTruthy();

    const search = new URL(requests.at(-1)?.url ?? "").searchParams;
    expect(search.get("q")).toBe("Dou");
    expect(search.get("types")).toBe("Person,Group");

    // Keyboard: the first option is Douris.
    await user.keyboard("{ArrowDown}");
    expect(input.getAttribute("aria-activedescendant")).toBe(option.id);
    await user.keyboard("{Enter}");
    expect(projected(doc).produced_by).toEqual({
      type: "Production",
      id: "<node>",
      carried_out_by: [
        { id: LEONARDO, type: "Person" },
        { id: DOURIS, type: "Person", _label: "Douris" },
      ],
    });
    expect(input).toHaveProperty("value", "");
    expect(screen.getByRole("listbox", { hidden: true }).hidden).toBe(true);
  });

  it("creates a stub record through the dialog and references it", async () => {
    const user = userEvent.setup();
    const requests = stubFetch({
      "GET /api/search/autocomplete": () => Response.json([]),
      "POST /api/records": () =>
        Response.json(
          { id: CREATED.split("/").at(-1), uri: CREATED, draft_epoch: 1 },
          { status: 201 },
        ),
    });
    const doc = draft();
    renderWidget(<EntityRefAutocomplete slot={CREATOR} base={null} />, doc);
    await user.type(await screen.findByRole("combobox"), "Anonymous Master");
    await user.click(await screen.findByRole("option", { name: /^Create new Group/ }));
    const dialog = await screen.findByRole("dialog", { name: "Create a new Group" });
    const name = within(dialog).getByLabelText("Name");
    expect(name).toHaveProperty("value", "Anonymous Master");
    await user.clear(name);
    await user.type(name, "Master of the Anonymous");
    await user.click(within(dialog).getByRole("button", { name: "Create and add" }));
    await screen.findByText("Master of the Anonymous");

    const post = requests.find((request) => request.method === "POST");
    expect(await post?.json()).toEqual({
      type: "Group",
      template: { identified_by: [{ type: "Name", content: "Master of the Anonymous" }] },
    });
    expect(projected(doc).produced_by).toEqual({
      type: "Production",
      id: "<node>",
      carried_out_by: [{ id: CREATED, type: "Group", _label: "Master of the Anonymous" }],
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("replaces the reference of a single-valued slot", async () => {
    const user = userEvent.setup();
    stubFetch({
      "GET /api/search/autocomplete": () =>
        Response.json([
          { id: DOURIS, type: "Person", label: "Douris", context: null, source: "index" },
        ]),
    });
    const doc = leonardo();
    renderWidget(<EntityRefAutocomplete slot={{ ...CREATOR, multiple: false }} base={null} />, doc);
    await user.type(await screen.findByRole("combobox"), "Dou");
    await user.click(await screen.findByRole("option", { name: /^Douris/ }));
    expect((projected(doc).produced_by as { carried_out_by: unknown }).carried_out_by).toEqual([
      { id: DOURIS, type: "Person", _label: "Douris" },
    ]);
  });
});
