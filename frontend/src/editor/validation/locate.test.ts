import { describe, expect, it } from "vitest";

import { messageParams } from "./messages.ts";
import { pointerSteps, pointerTokens, slotForDiagnostic, slotForSteps } from "./locate.ts";
import { finding, PROFILE } from "./testing.ts";

const URI = "https://data.test/id/1";
const DOCUMENT = {
  id: URI,
  type: "HumanMadeObject",
  identified_by: [
    { id: `${URI}/node/a`, type: "Identifier", content: "1990.1" },
    { id: `${URI}/node/b`, type: "Name", content: "" },
  ],
  produced_by: {
    id: `${URI}/node/c`,
    type: "Production",
    timespan: { id: `${URI}/node/d`, type: "TimeSpan", end_of_the_end: "x" },
  },
  made_of: [{ id: "http://vocab.getty.edu/aat/300015050", type: "Material" }],
  "a/b": { "c~d": 1 },
};

const slotOf = (path: string, code = "LAE-RULE-003", params: Record<string, unknown> = {}) =>
  slotForDiagnostic(PROFILE, DOCUMENT, finding(code, path, "violation", params))?.id ?? null;

describe("pointerSteps", () => {
  it("unescapes tokens and folds array indexes into the class of the item", () => {
    expect(pointerTokens("")).toEqual([]);
    expect(pointerTokens("/a~1b/c~0d")).toEqual(["a/b", "c~d"]);
    expect(pointerSteps(DOCUMENT, "/identified_by/1/content")).toEqual([
      { key: "identified_by", type: "Name" },
      { key: "content", type: null },
    ]);
    expect(pointerSteps(DOCUMENT, "/produced_by/timespan/end_of_the_end")).toEqual([
      { key: "produced_by", type: "Production" },
      { key: "timespan", type: "TimeSpan" },
      { key: "end_of_the_end", type: null },
    ]);
    // A property that is not there is still a step.
    expect(pointerSteps(DOCUMENT, "/dimension/0/value")).toEqual([
      { key: "dimension", type: null },
      { key: "value", type: null },
    ]);
  });
});

describe("slotForDiagnostic", () => {
  it("finds the slot whose class filter the item has", () => {
    expect(slotOf("/identified_by/1/content")).toBe("title");
    expect(slotOf("/identified_by/0/content")).toBe("identifiers");
  });

  it("prefers the slot that contains the place, the most specific one", () => {
    expect(slotOf("/produced_by/timespan/end_of_the_end")).toBe("production_date");
    expect(slotOf("/made_of/0")).toBe("materials");
  });

  it("falls back to the first slot below the place, in profile order", () => {
    expect(slotOf("/identified_by", "LAE-SHACL-002")).toBe("title");
    expect(slotOf("/produced_by", "LAE-SHACL-005")).toBe("creator");
  });

  it("looks for a missing property's slot first", () => {
    expect(slotOf("/produced_by", "LAE-SCHEMA-required", { missing: ["timespan"] })).toBe(
      "production_date",
    );
    expect(slotOf("/produced_by", "LAE-SCHEMA-required", { missing: ["nonsense"] })).toBe(
      "creator",
    );
  });

  it("finds nothing for the record itself or a property no slot edits", () => {
    expect(slotOf("", "LAE-SCHEMA-required", { missing: ["_label"] })).toBeNull();
    expect(slotOf("/a~1b/c~0d")).toBeNull();
    expect(slotForSteps(PROFILE, [])).toBeNull();
  });
});

describe("messageParams", () => {
  it("turns lists into a localized list and other values into JSON text", () => {
    expect(
      messageParams({ missing: ["_label", "type"], count: 3, value: { a: 1 }, text: "x" }, "en"),
    ).toEqual({ missing: "_label and type", count: 3, value: '{"a":1}', text: "x" });
  });
});
