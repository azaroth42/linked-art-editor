import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createScopedUndoManager } from "../crdt/useRecordDoc.ts";
import { stubLocaleFetch } from "../i18n/testing.ts";
import { EDTF_TYPE, formatInstant, parseEdtf, parseInstant } from "./edtfBounds.ts";
import { EdtfWidget } from "./EdtfWidget.tsx";
import { draft, ORIGIN, projected, renderWidget, slot } from "./testing.tsx";

const DATE = slot({
  id: "production_date",
  label: { en: "Production Date" },
  path: "produced_by.timespan",
  widget: "edtf",
  multiple: false,
});

const EDTF_NAME = { id: EDTF_TYPE, type: "Type" };

beforeEach(stubLocaleFetch);
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("parseEdtf", () => {
  it.each([
    ["1642", "1642-01-01T00:00:00Z", "1642-12-31T23:59:59Z"],
    ["1880~/1895?", "1880-01-01T00:00:00Z", "1895-12-31T23:59:59Z"],
    ["19XX", "1900-01-01T00:00:00Z", "1999-12-31T23:59:59Z"],
    ["1985-04-12T23:20:30Z", "1985-04-12T23:20:30Z", "1985-04-12T23:20:30Z"],
    // BCE: astronomical years, so 480 BCE is -0479 and 1 BCE is 0000.
    ["-0479/-0469", "-0479-01-01T00:00:00Z", "-0469-12-31T23:59:59Z"],
    ["0000", "0000-01-01T00:00:00Z", "0000-12-31T23:59:59Z"],
    ["-0001-03", "-0001-03-01T00:00:00Z", "-0001-03-31T23:59:59Z"],
    // An open end has no bound.
    ["../1900", null, "1900-12-31T23:59:59Z"],
    ["1900/..", "1900-01-01T00:00:00Z", null],
  ])("%s has the bounds %s and %s", (text, begin, end) => {
    expect(parseEdtf(text)).toEqual({ ok: true, begin, end });
  });

  it("rejects what edtf.js parse() rejects, and intervals that end first", () => {
    expect(parseEdtf("abc")).toEqual({ ok: false, error: "syntax" });
    expect(parseEdtf("1642-13")).toEqual({ ok: false, error: "syntax" });
    expect(parseEdtf("1900/1800")).toEqual({ ok: false, error: "range" });
  });

  it("formats and reads back xsd:dateTime with four-digit signed years", () => {
    const instant = parseInstant("-0479-01-01T00:00:00Z");
    expect(instant?.getUTCFullYear()).toBe(-479);
    expect(formatInstant(instant?.getTime() ?? NaN)).toBe("-0479-01-01T00:00:00Z");
    expect(parseInstant("1642-06-01T12:00:00+02:00")?.toISOString()).toBe(
      "1642-06-01T10:00:00.000Z",
    );
    expect(parseInstant("not a date")).toBeNull();
    expect(formatInstant(Number.POSITIVE_INFINITY)).toBeNull();
  });
});

describe("EdtfWidget", () => {
  it("writes the outer bounds and the EDTF name of a BCE interval in one undo step", async () => {
    const user = userEvent.setup();
    const doc = draft();
    const undo = createScopedUndoManager(doc, ORIGIN);
    renderWidget(<EdtfWidget slot={DATE} base={null} />, doc);
    const input = await screen.findByLabelText("Production Date");
    await user.click(input);
    await user.paste("-0479/-0469");
    expect(projected(doc).produced_by).toEqual({
      type: "Production",
      id: "<node>",
      timespan: {
        type: "TimeSpan",
        id: "<node>",
        begin_of_the_begin: "-0479-01-01T00:00:00Z",
        end_of_the_end: "-0469-12-31T23:59:59Z",
        identified_by: [
          { type: "Name", id: "<node>", content: "-0479/-0469", classified_as: [EDTF_NAME] },
        ],
      },
    });
    expect(screen.getByTestId("production_date-bounds").textContent).toMatch(
      /^Earliest Jan 1, 480 BC, latest Dec 31, 470 BC\.$/,
    );
    undo.undo();
    expect(projected(doc)).not.toHaveProperty("produced_by");
  });

  it("shows parse errors inline and writes nothing until the text is valid", async () => {
    const user = userEvent.setup();
    const doc = draft({
      produced_by: {
        type: "Production",
        timespan: {
          type: "TimeSpan",
          identified_by: [{ type: "Name", content: "1503", classified_as: [EDTF_NAME] }],
          begin_of_the_begin: "1503-01-01T00:00:00Z",
          end_of_the_begin: "1503-06-30T23:59:59Z",
          end_of_the_end: "1503-12-31T23:59:59Z",
        },
      },
    });
    renderWidget(<EdtfWidget slot={DATE} base={null} />, doc);
    const input = await screen.findByLabelText("Production Date");
    expect(input).toHaveProperty("value", "1503");
    const before = projected(doc);

    await user.clear(input);
    await user.type(input, "1503/1");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByText(/^This is not an EDTF date/)).toBeTruthy();
    await user.clear(input);
    await user.type(input, "1519/1503");
    expect(screen.getByText("This interval ends before it starts.")).toBeTruthy();
    // Only the empty text was written in between (clearing removes the dates)...
    expect(projected(doc)).not.toEqual(before);

    await user.clear(input);
    await user.type(input, "1503/1519");
    expect(input.getAttribute("aria-invalid")).toBe("false");
    // ...and the inner bounds the old value had are gone: the EDTF text is the whole span.
    expect((projected(doc).produced_by as { timespan: object }).timespan).toEqual({
      type: "TimeSpan",
      id: "<node>",
      identified_by: [
        { type: "Name", id: "<node>", content: "1503/1519", classified_as: [EDTF_NAME] },
      ],
      begin_of_the_begin: "1503-01-01T00:00:00Z",
      end_of_the_end: "1519-12-31T23:59:59Z",
    });
  });

  it("clearing the field removes the dates and the EDTF name, keeping other names", async () => {
    const user = userEvent.setup();
    const display = { type: "Name", content: "early 16th century" };
    const doc = draft({
      produced_by: {
        type: "Production",
        timespan: {
          type: "TimeSpan",
          identified_by: [display, { type: "Name", content: "15XX", classified_as: [EDTF_NAME] }],
          begin_of_the_begin: "1500-01-01T00:00:00Z",
          end_of_the_end: "1599-12-31T23:59:59Z",
        },
      },
    });
    renderWidget(<EdtfWidget slot={DATE} base={null} />, doc);
    await user.clear(await screen.findByLabelText("Production Date"));
    expect((projected(doc).produced_by as { timespan: object }).timespan).toEqual({
      type: "TimeSpan",
      id: "<node>",
      identified_by: [{ ...display, id: "<node>" }],
    });
    expect(screen.getByTestId("production_date-bounds").textContent).toBe("No dates recorded.");
  });
});
