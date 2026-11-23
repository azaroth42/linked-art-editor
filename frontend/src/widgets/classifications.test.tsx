import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UiProfile } from "../api/hooks/editing.ts";
import { stubLocaleFetch } from "../i18n/testing.ts";
import { classificationGroups, claims, partitionClassifications } from "./classifications.ts";
import { ClassificationWidget } from "./ClassificationWidget.tsx";
import { AAT, BUNDLE, draft, projected, renderWidget, slot } from "./testing.tsx";
import { findVocabulary, type Reference } from "./vocab.ts";

const PAINTING = `${AAT}300033618`;
const ARTWORK = `${AAT}300133025`;
const BAROQUE = `${AAT}300021140`;
const TYPE_OF_WORK = `${AAT}300435443`;
const STYLE = `${AAT}300015646`;

function reference(id: string, fields: Partial<Reference> = {}): Reference {
  return { id, type: "Type", label: null, metatypes: [], ...fields };
}

const vocabulary = (id: string) => {
  const found = findVocabulary(BUNDLE, id);
  if (found === null) {
    throw new Error(id);
  }
  return found;
};

const TYPE_OF_WORK_SLOT = slot({
  id: "type_of_work",
  label: { en: "Type of Work" },
  path: "classified_as",
  widget: "classification",
  value_set: "vocab:type_of_work",
});
const MATERIALS_SLOT = slot({
  id: "materials",
  label: { en: "Materials" },
  path: "made_of",
  widget: "classification",
  value_set: "vocab:material",
});
const PROFILE = {
  class: "HumanMadeObject",
  version: 1,
  sections: [
    {
      id: "all",
      label: { en: "All" },
      advanced: false,
      slots: [TYPE_OF_WORK_SLOT, MATERIALS_SLOT],
    },
  ],
} as UiProfile;

beforeEach(stubLocaleFetch);
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("claims", () => {
  it("claims pinned terms, inline metatypes of a rule, and the rule's classes", () => {
    const work = vocabulary("vocab:type_of_work");
    const material = vocabulary("vocab:material");
    expect(claims(work, reference(PAINTING))).toBe(true);
    expect(claims(work, reference(`${AAT}1`, { metatypes: [TYPE_OF_WORK] }))).toBe(true);
    expect(claims(work, reference(`${AAT}1`))).toBe(false);
    // Exclusions win, even over an inline metatype.
    expect(claims(work, reference(ARTWORK, { metatypes: [TYPE_OF_WORK] }))).toBe(false);
    // A rule with only classes claims every reference of those classes.
    expect(claims(material, reference(`${AAT}300012363`, { type: "Material" }))).toBe(true);
    expect(claims(material, reference(`${AAT}300012363`, { type: "Type" }))).toBe(false);
    // An enumerated set claims its terms only.
    expect(claims(vocabulary("vocab:work_title_types"), reference(`${AAT}1`))).toBe(false);
  });
});

describe("partitionClassifications", () => {
  it("puts each value on the first card that claims it and the rest in Other", () => {
    const values = [
      reference(PAINTING, { metatypes: [TYPE_OF_WORK] }),
      reference(ARTWORK),
      reference(BAROQUE),
      reference(`${AAT}2`, { metatypes: [STYLE, TYPE_OF_WORK] }),
    ];
    const { claimed, other } = partitionClassifications(values, [
      vocabulary("vocab:type_of_work"),
      vocabulary("vocab:style"),
    ]);
    expect(claimed.get("vocab:type_of_work")?.map((value) => value.id)).toEqual([
      PAINTING,
      `${AAT}2`,
    ]);
    expect(claimed.get("vocab:style")?.map((value) => value.id)).toEqual([BAROQUE]);
    expect(other.map((value) => value.id)).toEqual([ARTWORK]);
  });

  it("puts everything in Other when no Value Set is known (bundle not loaded)", () => {
    const { claimed, other } = partitionClassifications([reference(PAINTING)], []);
    expect(claimed.size).toBe(0);
    expect(other).toHaveLength(1);
  });
});

describe("classificationGroups", () => {
  it("orders slot cards first, then Value Sets bound to the path without a slot", () => {
    const groups = classificationGroups(PROFILE, BUNDLE);
    const classified = groups.get("classified_as");
    expect(classified?.vocabularies.map((vocab) => vocab.id)).toEqual([
      "vocab:type_of_work",
      "vocab:style",
    ]);
    expect(classified?.unslotted.map((vocab) => vocab.id)).toEqual(["vocab:style"]);
    expect(classified?.ownerSlotId).toBe("type_of_work");
    expect(groups.get("made_of")?.unslotted).toEqual([]);
  });
});

describe("ClassificationWidget", () => {
  it("renders a card per Value Set and Other classifications with remove only", async () => {
    const user = userEvent.setup();
    const doc = draft({
      classified_as: [
        { id: PAINTING, type: "Type", classified_as: [{ id: TYPE_OF_WORK, type: "Type" }] },
        { id: ARTWORK, type: "Type", _label: "Artwork" },
        { id: BAROQUE, type: "Type" },
      ],
    });
    renderWidget(<ClassificationWidget slot={TYPE_OF_WORK_SLOT} base={null} />, doc, {
      classificationGroups: classificationGroups(PROFILE, BUNDLE),
    });
    const work = await screen.findByRole("group", { name: "Type of Work" });
    expect(
      within(work).getByRole("button", { name: "painting" }).getAttribute("aria-pressed"),
    ).toBe("true");
    expect(within(work).getByRole("button", { name: "print" }).getAttribute("aria-pressed")).toBe(
      "false",
    );
    const style = screen.getByRole("group", { name: "Style" });
    expect(
      within(style).getByRole("button", { name: "Baroque" }).getAttribute("aria-pressed"),
    ).toBe("true");
    const other = screen.getByRole("group", { name: "Other classifications" });
    expect(within(other).queryByRole("button", { name: "Artwork" })).toBeNull();

    await user.click(within(work).getByRole("button", { name: "print" }));
    await user.click(within(work).getByRole("button", { name: "painting" }));
    await user.click(within(other).getByRole("button", { name: "Remove Artwork" }));
    expect(projected(doc).classified_as).toEqual([
      { id: BAROQUE, type: "Type" },
      { id: `${AAT}300041273`, type: "Type" },
    ]);
    expect(screen.queryByRole("group", { name: "Other classifications" })).toBeNull();
  });

  it("adds material references with the class of the rule", async () => {
    const user = userEvent.setup();
    const doc = draft({ made_of: [{ id: `${AAT}300012363`, type: "Material", _label: "poplar" }] });
    renderWidget(<ClassificationWidget slot={MATERIALS_SLOT} base={null} />, doc, {
      classificationGroups: classificationGroups(PROFILE, BUNDLE),
    });
    const card = await screen.findByRole("group", { name: "Materials" });
    // A rule member that is not pinned is shown pressed, after the pinned terms.
    expect(
      within(card)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["oil paint", "canvas", "poplar"]);
    await user.click(within(card).getByRole("button", { name: "canvas" }));
    expect(projected(doc).made_of).toEqual([
      { id: `${AAT}300012363`, type: "Material", _label: "poplar" },
      { id: `${AAT}300014078`, type: "Material" },
    ]);
  });
});
