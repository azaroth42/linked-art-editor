import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createI18n,
  DEFAULT_LANGUAGE,
  initialLanguage,
  localeUrl,
  NAMESPACES,
  PSEUDO_LANGUAGE,
  pseudoLocalize,
} from "./index.ts";
import { PSEUDO_EXPANSION } from "./pseudo.ts";
import { LOCALE_FILES, stubLocaleFetch } from "./testing.ts";

describe("namespaces", () => {
  it.each(NAMESPACES)("has an en file for %s", (namespace) => {
    expect(LOCALE_FILES[`../../public/locales/en/${namespace}.json`]).toBeDefined();
  });
});

describe("initialLanguage", () => {
  it("defaults to en", () => {
    expect(initialLanguage("")).toBe(DEFAULT_LANGUAGE);
  });

  it("accepts the pseudo locale outside production builds", () => {
    expect(initialLanguage("?lng=pseudo")).toBe(PSEUDO_LANGUAGE);
  });

  it("ignores unsupported languages", () => {
    expect(initialLanguage("?lng=xx")).toBe(DEFAULT_LANGUAGE);
  });
});

describe("localeUrl", () => {
  it("reads the pseudo locale from the en files", () => {
    expect(localeUrl("pseudo", "editor")).toBe("/locales/en/editor.json");
    expect(localeUrl("en", "errors")).toBe("/locales/en/errors.json");
  });
});

describe("pseudoLocalize", () => {
  it("wraps in brackets and accents letters", () => {
    expect(pseudoLocalize("Save")).toMatch(/^\[Šáṽé ~+\]$/);
  });

  it.each([
    "Save",
    "Linked Art Editor",
    "A much longer sentence that has to wrap on narrow screens.",
  ])("expands %j by at least 35%%", (text) => {
    expect(pseudoLocalize(text).length).toBeGreaterThanOrEqual(
      Math.ceil(text.length * (1 + PSEUDO_EXPANSION)),
    );
  });

  it("leaves ICU arguments and Trans tags intact", () => {
    const result = pseudoLocalize("Hello {name}, see <1>docs</1>");
    expect(result).toContain("{name}");
    expect(result).toContain("<1>");
    expect(result).toContain("</1>");
    expect(result).not.toContain("Hello");
  });
});

describe("createI18n", () => {
  beforeEach(stubLocaleFetch);
  afterEach(() => vi.unstubAllGlobals());

  it("formats ICU messages and applies the pseudo locale after formatting", async () => {
    const i18n = createI18n(PSEUDO_LANGUAGE);
    await i18n.changeLanguage(PSEUDO_LANGUAGE); // waits until the deferred init has loaded `common`
    i18n.addResource("en", "common", "count", "{n, plural, one {# record} other {# records}}");
    expect(i18n.t("count", { n: 2, lng: "en" })).toBe("2 records");
    expect(i18n.t("count", { n: 1 })).toMatch(/^\[1 ŕéçóŕð ~+\]$/);
  });
});
