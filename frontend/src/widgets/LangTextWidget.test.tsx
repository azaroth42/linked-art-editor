import { act, cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import { listEntries, spliceText } from "../crdt/paths.ts";
import { createScopedUndoManager } from "../crdt/useRecordDoc.ts";
import { stubLocaleFetch } from "../i18n/testing.ts";
import { LangTextWidget } from "./LangTextWidget.tsx";
import {
  AAT,
  ALTERNATE_TITLE,
  draft,
  ENGLISH,
  FRENCH,
  ORIGIN,
  PRIMARY_NAME,
  projected,
  renderWidget,
  slot,
} from "./testing.tsx";

const TITLE = slot({
  id: "title",
  label: { en: "Title" },
  path: "identified_by[Name]",
  widget: "langtext",
  value_set: "vocab:work_title_types",
});
const OTHER_TYPE = { id: `${AAT}300404669`, type: "Type", _label: "Display Title" };

function monaLisa(): Y.Doc {
  return draft({
    identified_by: [
      {
        type: "Name",
        classified_as: [{ id: PRIMARY_NAME, type: "Type" }, OTHER_TYPE],
        content: "Mona Lisa",
        language: [{ id: ENGLISH, type: "Language", _label: "English" }],
      },
      { type: "Identifier", content: "INV. 779" },
    ],
  });
}

beforeEach(stubLocaleFetch);
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LangTextWidget", () => {
  it("binds the content Y.Text to a text area, one undo step per edit", async () => {
    const user = userEvent.setup();
    const doc = monaLisa();
    const undo = createScopedUndoManager(doc, ORIGIN);
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc);
    const area = await screen.findByLabelText("Title 1");
    expect(area).toHaveProperty("value", "Mona Lisa");
    expect(area.getAttribute("dir")).toBe("auto");

    const entry = listEntries(doc, "identified_by[Name]")[0]?.[1];
    const text = entry?.get("content");
    await user.type(area, "!");
    expect(projected(doc).identified_by).toMatchObject([{ content: "Mona Lisa!" }, {}]);
    // `content` stays the same Y.Text, spliced rather than replaced.
    expect(entry?.get("content")).toBe(text);
    expect(text).toBeInstanceOf(Y.Text);
    undo.stopCapturing();
    undo.undo();
    expect(projected(doc).identified_by).toMatchObject([{ content: "Mona Lisa" }, {}]);
  });

  it("follows edits from another client", async () => {
    const doc = monaLisa();
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc);
    const area = await screen.findByLabelText("Title 1");
    const remote = new Y.Doc();
    Y.applyUpdate(remote, Y.encodeStateAsUpdate(doc));
    const base = listEntries(remote, "identified_by[Name]")[0]?.[1] ?? null;
    spliceText(remote, "content", 0, 0, "The ", { base });
    // The local caret sits after "Mona"; the remote insertion before it must not move it off.
    const field = area as HTMLTextAreaElement;
    field.focus();
    field.setSelectionRange(4, 4);
    act(() => Y.applyUpdate(doc, Y.encodeStateAsUpdate(remote)));
    expect(field.value).toBe("The Mona Lisa");
    expect([field.selectionStart, field.selectionEnd]).toEqual([8, 8]);
  });

  it("writes the language as an AAT concept chosen by BCP-47 tag", async () => {
    const user = userEvent.setup();
    const doc = monaLisa();
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc);
    const select = await screen.findByLabelText("Language of Title 1");
    expect(select).toHaveProperty("value", "en");
    await user.selectOptions(select, "fr");
    expect((projected(doc).identified_by as object[])[0]).toMatchObject({
      language: [{ id: FRENCH, type: "Language" }],
    });
    await user.selectOptions(select, "");
    expect((projected(doc).identified_by as object[])[0]).not.toHaveProperty("language");
  });

  it("replaces only the Name type its Value Set claims and keeps other types", async () => {
    const user = userEvent.setup();
    const doc = monaLisa();
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc);
    const select = await screen.findByLabelText("Type of Title 1");
    expect(select).toHaveProperty("value", PRIMARY_NAME);
    await user.selectOptions(select, ALTERNATE_TITLE);
    expect(
      (projected(doc).identified_by as { classified_as: object[] }[])[0]?.classified_as,
    ).toEqual([OTHER_TYPE, { id: ALTERNATE_TITLE, type: "Type" }]);
  });

  it("adds a name with the default type and removes one, leaving identifiers alone", async () => {
    const user = userEvent.setup();
    const doc = monaLisa();
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc);
    await user.click(await screen.findByRole("button", { name: "Add to Title" }));
    const added = await screen.findByLabelText("Title 2");
    await user.type(added, "La Joconde");
    expect(projected(doc).identified_by).toEqual([
      expect.objectContaining({ content: "Mona Lisa" }),
      { type: "Identifier", id: "<node>", content: "INV. 779" },
      {
        type: "Name",
        id: "<node>",
        content: "La Joconde",
        classified_as: [{ id: PRIMARY_NAME, type: "Type" }],
      },
    ]);
    await user.click(screen.getByRole("button", { name: "Remove Title 1" }));
    expect(projected(doc).identified_by).toEqual([
      { type: "Identifier", id: "<node>", content: "INV. 779" },
      expect.objectContaining({ content: "La Joconde" }),
    ]);
  });

  it("sets the display direction without storing it", async () => {
    const user = userEvent.setup();
    const doc = monaLisa();
    const before = projected(doc);
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc);
    await user.selectOptions(await screen.findByLabelText("Text direction of Title 1"), "rtl");
    expect(screen.getByLabelText("Title 1").getAttribute("dir")).toBe("rtl");
    expect(projected(doc)).toEqual(before);
  });

  it("is read-only when editing is disabled", async () => {
    const doc = monaLisa();
    renderWidget(<LangTextWidget slot={TITLE} base={null} />, doc, { disabled: true });
    expect(await screen.findByLabelText("Title 1")).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Add to Title" })).toHaveProperty("disabled", true);
  });
});
