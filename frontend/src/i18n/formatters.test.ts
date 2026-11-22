import { describe, expect, it } from "vitest";

import { formatDate, formatList, formatNumber, formatRelative, intlLocale } from "./formatters.ts";

const NOW = new Date("2026-11-22T12:00:00Z");
const HOUR = 3_600_000;
const DAY = 24 * HOUR;

describe("intlLocale", () => {
  it("formats the pseudo locale as en", () => {
    expect(intlLocale("pseudo")).toBe("en");
    expect(intlLocale("de")).toBe("de");
  });
});

describe("formatDate", () => {
  it("uses the locale's conventions", () => {
    const options: Intl.DateTimeFormatOptions = { dateStyle: "long", timeZone: "UTC" };
    expect(formatDate("en-US", NOW, options)).toBe("November 22, 2026");
    expect(formatDate("de", NOW, options)).toBe("22. November 2026");
    expect(formatDate("fr", "2026-11-22T12:00:00Z", options)).toBe("22 novembre 2026");
  });

  it("accepts ISO strings and epoch milliseconds", () => {
    const options: Intl.DateTimeFormatOptions = { timeZone: "UTC" };
    expect(formatDate("en-US", NOW.toISOString(), options)).toBe(
      formatDate("en-US", NOW.getTime(), options),
    );
  });

  it("formats the pseudo locale like en", () => {
    expect(formatDate("pseudo", NOW)).toBe(formatDate("en", NOW));
  });

  it("rejects an invalid date", () => {
    expect(() => formatDate("en", "not a date")).toThrow(RangeError);
  });
});

describe("formatRelative", () => {
  it.each([
    [0, "now"],
    [-30_000, "30 seconds ago"],
    [-5 * 60_000, "5 minutes ago"],
    [3 * HOUR, "in 3 hours"],
    [-DAY, "yesterday"],
    [5 * DAY, "in 5 days"],
    [-14 * DAY, "2 weeks ago"],
    [60 * DAY, "in 2 months"],
    [-400 * DAY, "last year"],
  ])("formats an offset of %i ms as %j", (offset, expected) => {
    expect(formatRelative("en", NOW.getTime() + offset, NOW)).toBe(expected);
  });

  it("uses the locale", () => {
    expect(formatRelative("de", NOW.getTime() + 5 * DAY, NOW)).toBe("in 5 Tagen");
  });
});

describe("formatNumber", () => {
  it("uses the locale's separators", () => {
    expect(formatNumber("en", 1234.5)).toBe("1,234.5");
    expect(formatNumber("de", 1234.5)).toBe("1.234,5");
  });

  it("passes options through", () => {
    expect(formatNumber("en", 0.25, { style: "percent" })).toBe("25%");
  });
});

describe("formatList", () => {
  it("joins with the locale's conjunction", () => {
    expect(formatList("en", ["Editor", "Collaborator", "Viewer"])).toBe(
      "Editor, Collaborator, and Viewer",
    );
    expect(formatList("de", ["Editor", "Viewer"])).toBe("Editor und Viewer");
  });
});
