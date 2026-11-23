/**
 * The pure rules (mirror of `backend/tests/projection/test_rules.py` where the rule has a
 * TypeScript side): keys, the code-point comparator, `_order` arithmetic, text splices, plain
 * values.
 */
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import { hydrate, isInternalNodeIri } from "./hydrate.ts";
import { PathSyntaxError, parsePath } from "./pathSyntax.ts";
import { project, sortedEntries } from "./project.ts";
import {
  canonicalJson,
  compareCodePoints,
  decodePlain,
  encodePlain,
  entryKey,
  isNode,
  isReference,
  orderBetween,
  parseKey,
  propertyKey,
  ProjectionError,
  textChange,
  utf16Offset,
  valueKey,
} from "./rules.ts";

const URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";

function code(call: () => unknown): string {
  try {
    call();
  } catch (error) {
    if (error instanceof ProjectionError) {
      return error.code;
    }
    throw error;
  }
  throw new Error("no ProjectionError");
}

describe("classification (R3)", () => {
  it("needs a string id for a reference and a type for a node", () => {
    expect(isReference({ id: "x" })).toBe(true);
    expect(isReference({ id: "x", type: "T", _label: "l" })).toBe(true);
    expect(isReference({ id: "x", type: "T", content: "c" })).toBe(false);
    expect(isReference({ id: 5 })).toBe(false);
    expect(isNode({ type: "T" })).toBe(true);
    expect(isNode({ type: "T", id: 5 })).toBe(false);
    expect(isNode({ "@type": "T" })).toBe(false);
    expect(isNode(["type"])).toBe(false);
  });

  it("never reads a key from the prototype chain", () => {
    expect(isNode({ id: "x" })).toBe(false);
    const proto = JSON.parse('{"__proto__": {"type": "T"}, "constructor": 1}') as object;
    expect(isNode(proto)).toBe(false);
    expect(isReference(JSON.parse('{"id": "x", "__proto__": 1}'))).toBe(false);
  });
});

describe("keys (R6, R7)", () => {
  it("builds the same key text as Python's json.dumps(ensure_ascii=False)", () => {
    // Pairs from the spike's interop check: quotes, backslashes, control characters, U+2028,
    // non-BMP characters and empty strings.
    expect(entryKey("identified_by", "https://x/1")).toBe('["identified_by","https://x/1"]');
    expect(entryKey('a"b', "c\\d")).toBe('["a\\"b","c\\\\d"]');
    expect(entryKey("\n\t\u0001", " 😀")).toBe('["\\n\\t\\u0001"," 😀"]');
    expect(entryKey("", "")).toBe('["",""]');
    expect(propertyKey("[")).toBe('["["]');
    expect(propertyKey("plain")).toBe("plain");
  });

  it("parses entry keys and escaped property keys, and rejects anything else", () => {
    expect(parseKey('["a","b"]')).toEqual(["a", "b"]);
    expect(parseKey('["[x"]')).toEqual(["[x", null]);
    expect(parseKey("plain")).toEqual(["plain", null]);
    for (const bad of ["[", "[1]", '["a","b","c"]', "[]", '["a",1]', '[{"a":1}]']) {
      expect(code(() => parseKey(bad))).toBe("LAE-PROJ-005");
    }
  });

  it("tags opaque item keys by type", () => {
    expect(valueKey("1")).toBe("=s:1");
    expect(valueKey(1)).toBe("=n:1");
    expect(valueKey(33.5)).toBe("=n:33.5");
    expect(valueKey(true)).toBe("=b:true");
    expect(valueKey(null)).toBe("=z");
    expect(valueKey({ b: 1, a: [2] })).toBe('=j:{"a":[2],"b":1}');
  });
});

describe("code-point order (R13)", () => {
  it("sorts by code point, not by UTF-16 code unit", () => {
    // U+1F600 is above U+FF5E by code point, below it by UTF-16 code unit (0xD83D < 0xFF5E).
    expect(["\u{1F600}", "～", "a", "b"].sort(compareCodePoints)).toEqual([
      "a",
      "b",
      "～",
      "\u{1F600}",
    ]);
    expect(compareCodePoints("ab", "a")).toBeGreaterThan(0);
    expect(compareCodePoints("", "")).toBe(0);
  });

  it("breaks an _order tie by the whole entry key in code-point order", () => {
    const doc = hydrate({ id: URI, type: "HumanMadeObject" });
    const root = doc.getMap<unknown>("record");
    for (const id of ["https://x/～", "https://x/\u{1F600}", "https://x/a"]) {
      root.set(
        entryKey("classified_as", id),
        new Y.Map([
          ["id", id],
          ["_order", 0],
        ]),
      );
    }
    expect(sortedEntries(root, "classified_as").map(([key]) => parseKey(key)[1])).toEqual([
      "https://x/a",
      "https://x/～",
      "https://x/\u{1F600}",
    ]);
  });

  it("canonical JSON sorts keys by code point", () => {
    expect(canonicalJson({ "\u{1F600}": 1, "～": 2, a: [3, { d: 4, c: null }] })).toBe(
      '{"a":[3,{"c":null,"d":4}],"～":2,"\u{1F600}":1}',
    );
  });
});

describe("_order arithmetic (R12)", () => {
  it("takes 0, first - 1, last + 1 and midpoints, and refuses exhausted gaps", () => {
    expect(orderBetween(null, null)).toBe(0);
    expect(orderBetween(null, 0)).toBe(-1);
    expect(orderBetween(2, null)).toBe(3);
    expect(orderBetween(0, 1)).toBe(0.5);
    expect(orderBetween(0.5, 0.5)).toBeNull();
    expect(orderBetween(1 - 2 ** -53, 1)).toBeNull();
    expect(orderBetween(Number.MAX_VALUE, null)).toBeNull();
    expect(orderBetween(null, -Number.MAX_VALUE)).toBeNull();
  });
});

describe("text positions (R19, R23)", () => {
  it("converts code-point positions to UTF-16 offsets", () => {
    expect([0, 1, 2, 3, 4].map((i) => utf16Offset("aé😀𝒳", i))).toEqual([0, 1, 2, 4, 6]);
    expect(utf16Offset("", 0)).toBe(0);
  });

  it.each([
    ["x\u{1F600}y", "x\u{1F601}y", [1, 1, "\u{1F601}"]], // same high surrogate
    ["\u{1F600}z", "\u{1FA00}z", [0, 1, "\u{1FA00}"]], // same low surrogate
    ["café", "café", [3, 2, "é"]], // not normalized
    ["", "", [0, 0, ""]],
    ["", "a", [0, 0, "a"]],
    ["a", "", [0, 1, ""]],
    ["aaa", "aa", [2, 1, ""]], // the prefix is taken first
    ["aXa", "aa", [1, 1, ""]],
  ])("textChange(%j, %j) cuts between code points", (old, next, splice) => {
    expect(textChange(old, next)).toEqual(splice);
  });

  it("is the one splice between two strings", () => {
    const alphabet = ["a", "b", "😀", "𝒳", "é", "é"];
    let seed = 7;
    const random = () => {
      seed = (seed * 48271) % 2147483647;
      return seed / 2147483647;
    };
    const word = () =>
      Array.from(
        { length: Math.floor(random() * 8) },
        () => alphabet[Math.floor(random() * 6)],
      ).join("");
    for (let i = 0; i < 500; i += 1) {
      const old = word();
      const next = word();
      const [index, deleteCount, insert] = textChange(old, next);
      const points = Array.from(old);
      expect(
        points.slice(0, index).join("") + insert + points.slice(index + deleteCount).join(""),
      ).toBe(next);
      if (old === next) {
        expect([deleteCount, insert]).toEqual([0, ""]);
      }
    }
  });
});

describe("plain values (R9)", () => {
  it("stores numbers as Y numbers and objects as _json text", () => {
    expect(encodePlain(5)).toBe(5);
    expect(encodePlain(33.5)).toBe(33.5);
    expect(encodePlain(-0)).toBe(0);
    expect(encodePlain({ b: [1], a: "x" })).toEqual({ _json: '{"a":"x","b":[1]}' });
    expect(encodePlain([])).toEqual({ _json: "[]" });
    expect(code(() => encodePlain(Number.NaN))).toBe("LAE-PROJ-002");
    expect(code(() => encodePlain(Infinity))).toBe("LAE-PROJ-002");
    expect(code(() => encodePlain("\ud800"))).toBe("LAE-PROJ-003");
    expect(code(() => encodePlain(undefined))).toBe("LAE-PROJ-005");
    expect(code(() => encodePlain(10n))).toBe("LAE-PROJ-005");
  });

  it("decodes only what encoding produces", () => {
    expect(decodePlain({ _json: '{"a":[1,2.5,null]}' })).toEqual({ a: [1, 2.5, null] });
    for (const bad of [{ _json: "{" }, { _json: 1 }, { _json: "1", x: 1 }, [1], new Y.Text()]) {
      expect(code(() => decodePlain(bad))).toBe("LAE-PROJ-005");
    }
  });

  it("projects a __proto__ key as data, never as a prototype", () => {
    const input = JSON.parse(
      `{"id": "${URI}", "type": "HumanMadeObject", "__proto__": {"id": "https://x/p", "type": "Polluted"},
        "constructor": "c", "x_opaque": {"__proto__": [1]}}`,
    ) as Record<string, unknown>;
    const projected = project(hydrate(input));
    expect(Object.getPrototypeOf(projected)).toBe(Object.prototype);
    expect(Object.hasOwn(projected, "__proto__")).toBe(true);
    expect(canonicalJson(projected)).toBe(canonicalJson(input));
    expect(({} as Record<string, unknown>).type).toBeUndefined();
  });
});

describe("internal node IRIs and paths", () => {
  it("matches only canonical UUIDv7 IRIs of the record (R15)", () => {
    expect(isInternalNodeIri(`${URI}/node/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f`, URI)).toBe(true);
    expect(isInternalNodeIri(`${URI}/node/0192F0C1-7B1E-7C3A-9D2E-5A1B2C3D4E5F`, URI)).toBe(false);
    expect(isInternalNodeIri(`${URI}/node/0192f0c1-7b1e-4c3a-9d2e-5a1b2c3d4e5f`, URI)).toBe(false);
    expect(isInternalNodeIri(`${URI}x/node/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f`, URI)).toBe(false);
    expect(isInternalNodeIri(5, URI)).toBe(false);
  });

  it.each(["", "identified_by[0]", "a..b", "_label", "a.[B]", "A"])(
    "rejects the malformed path %j",
    (path) => {
      expect(() => parsePath(path)).toThrow(PathSyntaxError);
    },
  );

  it("parses segments and class filters", () => {
    expect(parsePath("identified_by[Name].part[Name].classified_as")).toEqual([
      { key: "identified_by", typeFilter: "Name" },
      { key: "part", typeFilter: "Name" },
      { key: "classified_as", typeFilter: null },
    ]);
  });
});
