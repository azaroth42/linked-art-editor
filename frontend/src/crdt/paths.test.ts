/**
 * Path operations (`paths.ts`): the TypeScript side of `backend/tests/projection/test_ops.py`.
 * Each test mirrors the Python test of the same name; the shared fixtures are in
 * `fixtures.test.ts`.
 */
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import { hydrate, isInternalNodeIri } from "./hydrate.ts";
import { parsePath } from "./pathSyntax.ts";
import {
  addNode,
  addReference,
  getEntry,
  getPath,
  insertItem,
  listEntries,
  moveEntry,
  removeEntry,
  removeValue,
  setObject,
  setScalar,
  spliceText,
  valueToJson,
  type YMap,
} from "./paths.ts";
import { project, recordMap, sortedEntries, stripInternalIris } from "./project.ts";
import {
  canonicalJson,
  entryKey,
  isObject,
  type JsonObject,
  ORDER_KEY,
  parseKey,
  ProjectionError,
  SINGLE_VALUED_CLASSES,
} from "./rules.ts";
import { syncDocs } from "./testing.ts";

const URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const AAT = "http://vocab.getty.edu/aat/";

const CORPUS = import.meta.glob<string>("../../../tests/corpus/*/*.json", {
  eager: true,
  query: "?raw",
  import: "default",
});

function record(properties: JsonObject = {}): JsonObject {
  return { id: URI, type: "HumanMadeObject", ...properties };
}

function names(...contents: string[]): JsonObject[] {
  return contents.map((content) => ({ type: "Name", content }));
}

function fork(doc: Y.Doc): Y.Doc {
  const other = new Y.Doc();
  Y.applyUpdate(other, Y.encodeStateAsUpdate(doc));
  return other;
}

function contents(doc: Y.Doc, path = "identified_by.content"): unknown[] {
  return getPath(doc, path).map(valueToJson);
}

function keys(doc: Y.Doc, path: string): string[] {
  return listEntries(doc, path).map(([key]) => key);
}

const never = (): string => {
  throw new Error("minted");
};

/** `call` throws `expected` and leaves no trace in the document. */
function expectRejected(doc: Y.Doc, expected: string, call: () => unknown): void {
  const before = Y.encodeStateAsUpdate(doc);
  let code: string | null = null;
  try {
    call();
  } catch (error) {
    if (!(error instanceof ProjectionError)) {
      throw error;
    }
    code = error.code;
  }
  expect(code).toBe(expected);
  expect(Y.encodeStateAsUpdate(doc)).toEqual(before);
}

/** The "Reading" rules of profiles/README.md applied to plain JSON. */
function evaluate(value: unknown, path: string): unknown[] {
  let current = [value];
  for (const segment of parsePath(path)) {
    let found: unknown[] = [];
    for (const item of current) {
      if (isObject(item) && Object.hasOwn(item, segment.key)) {
        const held = item[segment.key];
        found.push(...(Array.isArray(held) ? (held as unknown[]) : [held]));
      }
    }
    if (segment.typeFilter !== null) {
      found = found.filter((f) => isObject(f) && f.type === segment.typeFilter);
    }
    current = found;
  }
  return current;
}

const PROFILE_PATHS = [
  "identified_by[Name].content",
  "identified_by[Identifier].content",
  "identified_by[Name].part[Name].classified_as",
  "identified_by.language.notation",
  "dimension.unit",
  "dimension.value",
  "produced_by.timespan.begin_of_the_begin",
  "produced_by.part[Production].carried_out_by",
  "referred_to_by.classified_as.classified_as",
  "member_of[Set]",
  "equivalent",
  "born.timespan.end_of_the_end",
  "no_such_key.content",
  "type.content",
  "classified_as",
  "made_of",
  "referred_to_by[LinguisticObject].content",
  "produced_by.carried_out_by",
  "defined_by",
  // The slot paths of profiles/ui/*.yaml.
  "dimension",
  "identified_by[Identifier]",
  "identified_by[Name]",
  "produced_by.timespan",
  "produced_by.took_place_at",
  "referred_to_by[LinguisticObject]",
];

describe("the golden corpus", () => {
  const files = Object.entries(CORPUS);

  it("round-trips through the TypeScript hydrate and project", () => {
    expect(files.length).toBeGreaterThanOrEqual(244);
    for (const [path, text] of files) {
      const source = JSON.parse(text) as JsonObject;
      const doc = hydrate(source);
      const projected = project(doc);
      expect(canonicalJson(stripInternalIris(projected)), path).toBe(canonicalJson(source));
      // A second replica built from the update projects identically.
      expect(canonicalJson(project(fork(doc))), path).toBe(canonicalJson(projected));
    }
  });

  it("getPath equals evaluating the path on the projection", () => {
    let addressed = 0;
    for (const [file, text] of files) {
      const doc = hydrate(JSON.parse(text));
      const projected = project(doc);
      for (const path of PROFILE_PATHS) {
        const got = getPath(doc, path).map(valueToJson);
        expect(canonicalJson(got), `${file} ${path}`).toBe(
          canonicalJson(evaluate(projected, path)),
        );
        addressed += got.length;
      }
    }
    expect(addressed).toBeGreaterThan(800);
  });
});

describe("getPath", () => {
  it("returns live values and reads inside opaque values", () => {
    const doc = hydrate(
      record({
        identified_by: names("a"),
        timespan: { type: "TimeSpan", begin_of_the_begin: "1500" },
        notation: ["x", 1, { k: [{ type: "T", v: 1 }, { v: 2 }] }],
        x_opaque: { inner: { deep: [1, 2.5, null] }, id: 5 },
        x_empty: [],
      }),
    );
    const [entry] = getPath(doc, "identified_by[Name]") as YMap[];
    expect(entry).toBeInstanceOf(Y.Map);
    expect(entry?.has(ORDER_KEY)).toBe(true);
    expect(valueToJson(entry)).toEqual({ id: entry?.get("id"), type: "Name", content: "a" });
    const [text] = getPath(doc, "identified_by.content");
    expect(text).toBeInstanceOf(Y.Text);
    const [span] = getPath(doc, "timespan") as YMap[];
    expect(span?.has(ORDER_KEY)).toBe(false);
    expect(getPath(doc, "timespan.begin_of_the_begin")).toEqual(["1500"]);
    expect(getPath(doc, "notation")).toEqual(["x", 1, { k: [{ type: "T", v: 1 }, { v: 2 }] }]);
    expect(getPath(doc, "notation.k.v")).toEqual([1, 2]);
    expect(getPath(doc, "notation.k[T].v")).toEqual([1]);
    expect(getPath(doc, "x_opaque.inner.deep")).toEqual([1, 2.5, null]);
    expect(getPath(doc, "x_empty")).toEqual([]);
    expect(getPath(doc, "identified_by.content.more")).toEqual([]);
    expect(getPath(doc, "content", { base: entry }).map(valueToJson)).toEqual(["a"]);
    expect(getPath(doc, "identified_by[Type]")).toEqual([]);
  });

  it("prefers entries over a single value", () => {
    const doc = hydrate(record());
    const other = fork(doc);
    setScalar(doc, "x_both", "single");
    insertItem(other, recordMap(other), "x_both", "entry");
    syncDocs([doc, other]);
    expect(recordMap(doc).get("x_both")).toBe("single");
    expect(getPath(doc, "x_both")).toEqual(["entry"]);
    expect(project(doc).x_both).toEqual(["entry"]);
  });
});

describe("listEntries and getEntry", () => {
  it("lists entries in order, filtered by class", () => {
    const doc = hydrate(
      record({
        identified_by: [...names("a"), { type: "Identifier", content: "i" }, ...names("b")],
        notation: ["x", "x"],
      }),
    );
    const every = listEntries(doc, "identified_by");
    expect(every.map(([, e]) => e.get("type"))).toEqual(["Name", "Identifier", "Name"]);
    const third = (every[2] as [string, YMap])[0];
    expect(keys(doc, "identified_by[Identifier]")).toEqual([(every[1] as [string, YMap])[0]]);
    expect(String(getEntry(doc, "identified_by", third).get("content"))).toBe("b");
    expect(getEntry(doc, "identified_by[Type]", third).get("type")).toBe("Name");
    expect(keys(doc, "notation")).toEqual([
      entryKey("notation", "=s:x"),
      entryKey("notation", "=s:x #2"),
    ]);
    expect(listEntries(doc, "notation[Name]")).toEqual([]);
    expect(listEntries(doc, "produced_by.part")).toEqual([]);
    expect(listEntries(doc, "x_absent")).toEqual([]);
    const first = (every[0] as [string, YMap])[0];
    for (const [path, key] of [
      ["identified_by", entryKey("identified_by", "missing")],
      ["classified_as", first],
      ["identified_by", "identified_by"],
      ["produced_by.identified_by", first],
    ] as const) {
      expectRejected(doc, "LAE-PROJ-008", () => getEntry(doc, path, key));
    }
    expectRejected(doc, "LAE-PROJ-007", () => listEntries(doc, "identified_by.part"));
  });
});

describe("addNode", () => {
  it("mints IRIs and appends", () => {
    const doc = hydrate(record({ identified_by: names("a", "b") }));
    const key = addNode(doc, "identified_by", {
      type: "Name",
      content: "c",
      part: [{ type: "Name", content: "p" }],
    });
    const [prop, nodeId] = parseKey(key);
    expect(prop).toBe("identified_by");
    expect(isInternalNodeIri(nodeId, URI)).toBe(true);
    const entry = getEntry(doc, "identified_by", key);
    expect(entry.get(ORDER_KEY)).toBe(2);
    expect(entry.get("content")).toBeInstanceOf(Y.Text);
    expect(contents(doc)).toEqual(["a", "b", "c"]);
    const names_ = project(doc).identified_by as JsonObject[];
    const [part] = names_[2]?.part as JsonObject[];
    expect(isInternalNodeIri(part?.id, URI)).toBe(true);
    expect(part?.id).not.toBe(nodeId);
    expect((stripInternalIris(project(doc)).identified_by as unknown[])[2]).toEqual({
      type: "Name",
      content: "c",
      part: [{ type: "Name", content: "p" }],
    });
  });

  it("positions and types", () => {
    const doc = hydrate(record({ identified_by: names("a", "b") }));
    const [first, second] = keys(doc, "identified_by") as [string, string];
    addNode(doc, "identified_by[Name]", { content: "mid" }, { beforeKey: second });
    addNode(doc, "identified_by[Identifier]", { content: "front" }, { beforeKey: first });
    addNode(doc, "identified_by[Name]", { type: "Name", content: "end" });
    expect(contents(doc)).toEqual(["front", "a", "mid", "b", "end"]);
    const entries = listEntries(doc, "identified_by");
    expect(entries.map(([, e]) => e.get(ORDER_KEY))).toEqual([-1, 0, 0.5, 1, 2]);
    expect(entries[0]?.[1].get("type")).toBe("Identifier");
  });

  it("keeps an id it is given and never duplicates it", () => {
    const doc = hydrate(record());
    const minted: string[] = [];
    const mint = () => {
      minted.push(`${URI}/node/${String(minted.length)}`);
      return minted[minted.length - 1] as string;
    };
    const node = { id: "https://example.org/own", type: "Type", part: [{ type: "Type" }] };
    const key = addNode(doc, "classified_as", node, { mint });
    expect(key).toBe(entryKey("classified_as", "https://example.org/own"));
    expect(minted).toHaveLength(1);
    const before = Y.encodeStateAsUpdate(doc);
    expect(addNode(doc, "classified_as", { ...node, _label: "again" }, { mint })).toBe(key);
    expect(Y.encodeStateAsUpdate(doc)).toEqual(before);
    expect(minted).toHaveLength(1);
    expect(node).toEqual({ id: "https://example.org/own", type: "Type", part: [{ type: "Type" }] });
  });

  it("rejections change nothing and mint nothing", () => {
    const doc = hydrate(
      record({ identified_by: names("a"), timespan: { type: "TimeSpan" }, x_single: "value" }),
    );
    const [first] = keys(doc, "identified_by") as [string];
    const cases: [string, () => unknown][] = [
      ["LAE-PROJ-007", () => addNode(doc, "identified_by", { content: "no type" })],
      ["LAE-PROJ-007", () => addNode(doc, "identified_by[Name]", { type: "Type" })],
      ["LAE-PROJ-007", () => addNode(doc, "identified_by", { type: "Name", id: 5 })],
      [
        "LAE-PROJ-007",
        () => addNode(doc, "identified_by", ["not", "an", "object"] as unknown as JsonObject),
      ],
      ["LAE-PROJ-006", () => addNode(doc, "timespan", { type: "TimeSpan" }, { mint: never })],
      ["LAE-PROJ-006", () => addNode(doc, "x_single", { type: "Name" }, { mint: never })],
      [
        "LAE-PROJ-001",
        () => addNode(doc, "identified_by", { type: "Name", _order: 1 }, { mint: never }),
      ],
      [
        "LAE-PROJ-001",
        () =>
          addNode(
            doc,
            "identified_by",
            { type: "Name", part: [{ type: "Name", _value: 1 }] },
            { mint: never },
          ),
      ],
      [
        "LAE-PROJ-002",
        () => addNode(doc, "identified_by", { type: "Name", x: Number.NaN }, { mint: never }),
      ],
      [
        "LAE-PROJ-003",
        () => addNode(doc, "identified_by", { type: "Name", content: "\ud800" }, { mint: never }),
      ],
      [
        "LAE-PROJ-008",
        () =>
          addNode(
            doc,
            "identified_by[Name]",
            {},
            { beforeKey: entryKey("identified_by", "x"), mint: never },
          ),
      ],
      [
        "LAE-PROJ-008",
        () => addNode(doc, "classified_as[Type]", {}, { beforeKey: first, mint: never }),
      ],
      [
        "LAE-PROJ-008",
        () =>
          addNode(
            doc,
            "produced_by.part[Production]",
            {},
            { beforeKey: entryKey("part", "x"), mint: never },
          ),
      ],
      ["LAE-PROJ-007", () => addNode(doc, "shows.part[Name]", {}, { mint: never })],
      ["LAE-PROJ-006", () => addNode(doc, "x_single[Name].part[Name]", {}, { mint: never })],
    ];
    for (const [expected, call] of cases) {
      expectRejected(doc, expected, call);
    }
  });

  it("without a root id, minting needs the record URI", () => {
    const doc = hydrate({ type: "HumanMadeObject" }, URI);
    expectRejected(doc, "LAE-PROJ-004", () => addNode(doc, "identified_by[Name]", {}));
    const key = addNode(doc, "identified_by[Name]", {}, { recordUri: URI });
    expect(isInternalNodeIri(parseKey(key)[1], URI)).toBe(true);
    // A reference needs no IRI, so no record URI either.
    addReference(doc, "classified_as", AAT + "1", { type: "Type" });
  });
});

describe("addReference", () => {
  it("adds, positions, labels and never duplicates a reference", () => {
    const doc = hydrate(record({ classified_as: [{ id: AAT + "1", type: "Type" }] }));
    const first = entryKey("classified_as", AAT + "1");
    const key = addReference(doc, "classified_as", AAT + "2", { type: "Type", label: "two" });
    expect(key).toBe(entryKey("classified_as", AAT + "2"));
    expect(addReference(doc, "classified_as[Type]", AAT + "3", { beforeKey: first })).toBe(
      entryKey("classified_as", AAT + "3"),
    );
    expect(addReference(doc, "x_untyped", "https://example.org/u")).toBe(
      entryKey("x_untyped", "https://example.org/u"),
    );
    expect(project(doc).classified_as).toEqual([
      { id: AAT + "3", type: "Type" },
      { id: AAT + "1", type: "Type" },
      { id: AAT + "2", type: "Type", _label: "two" },
    ]);
    expect(project(doc).x_untyped).toEqual([{ id: "https://example.org/u" }]);
    const before = Y.encodeStateAsUpdate(doc);
    expect(addReference(doc, "classified_as", AAT + "1", { type: "Type", label: "x" })).toBe(first);
    expect(addReference(doc, "classified_as", AAT + "1", { type: "Other", beforeKey: key })).toBe(
      first,
    );
    expect(Y.encodeStateAsUpdate(doc)).toEqual(before);
    expectRejected(doc, "LAE-PROJ-007", () =>
      addReference(doc, "classified_as[Type]", AAT + "4", { type: "Set" }),
    );
    expectRejected(doc, "LAE-PROJ-007", () =>
      addReference(doc, "classified_as", 5 as unknown as string),
    );
    expectRejected(doc, "LAE-PROJ-003", () =>
      addReference(doc, "classified_as", "\udc00", { type: "Type" }),
    );
  });

  it("two clients adding the same reference converge on one entry", () => {
    const a = hydrate(record());
    const b = fork(a);
    addReference(a, "classified_as", AAT + "1", { type: "Type", label: "from a" });
    addReference(b, "classified_as", AAT + "1", { type: "Type", label: "from b" });
    addReference(b, "classified_as", AAT + "2", { type: "Type" });
    syncDocs([a, b]);
    expect(canonicalJson(project(a))).toBe(canonicalJson(project(b)));
    const found = project(a).classified_as as JsonObject[];
    expect(found.map((r) => r.id).sort()).toEqual([AAT + "1", AAT + "2"]);
  });
});

describe("concurrent inserts (R14, R18)", () => {
  it.each([true, false])("two clients insert a name at position 1 (a first: %s)", (aFirst) => {
    const a = hydrate(record({ identified_by: names("n0", "n1", "n2") }));
    const b = fork(a);
    const second = keys(a, "identified_by")[1] as string;
    const keyA = addNode(a, "identified_by[Name]", { content: "A" }, { beforeKey: second });
    const keyB = addNode(b, "identified_by[Name]", { content: "B" }, { beforeKey: second });
    syncDocs(aFirst ? [a, b] : [b, a]);
    expect(canonicalJson(project(a))).toBe(canonicalJson(project(b)));
    const order = contents(a);
    expect(order[0]).toBe("n0");
    expect(order.slice(3)).toEqual(["n1", "n2"]);
    expect(order.slice(1, 3)).toEqual(keyA < keyB ? ["A", "B"] : ["B", "A"]);
    const orders = listEntries(a, "identified_by").map(([, e]) => e.get(ORDER_KEY));
    expect(orders).toEqual([0, 0.5, 0.5, 1, 2]);
  });

  const FIRST_INSERT_PATHS = [
    "referred_to_by[LinguisticObject]",
    "produced_by.part[Production]",
    "identified_by[Name].part[Name]",
    "timespan.identified_by[Name]",
    "x_empty[Name]",
  ];

  for (const clients of [2, 3]) {
    it.each(FIRST_INSERT_PATHS)(
      `concurrent first inserts into %s are all kept (${String(clients)} clients)`,
      (path) => {
        const source = hydrate(
          record({
            identified_by: names("title"),
            produced_by: { type: "Production" },
            timespan: { type: "TimeSpan" },
            x_empty: [],
          }),
        );
        const docs = [source, ...Array.from({ length: clients - 1 }, () => fork(source))];
        expect(listEntries(source, path)).toEqual([]);
        const plain = path.replace(/\[[A-Za-z]+\]$/, "");
        const added = docs.map((doc, number) => {
          const key = addNode(doc, path, { _label: `from ${String(number)}` });
          addReference(doc, plain, "https://example.org/shared");
          return key;
        });
        expect(new Set(added).size).toBe(clients);
        syncDocs([...docs].reverse());
        expect(new Set(docs.map((doc) => canonicalJson(project(doc)))).size).toBe(1);
        for (const doc of docs) {
          const found = keys(doc, plain);
          expect(found).toEqual(expect.arrayContaining(added));
          expect(found).toHaveLength(clients + 1);
          const labels = getPath(doc, path).map((v) => (v as YMap).get("_label"));
          expect(labels.sort()).toEqual(docs.map((_, i) => `from ${String(i)}`));
        }
      },
    );
  }

  it("concurrent creation of a missing intermediate follows the kind of property", () => {
    const a = hydrate(record());
    const b = fork(a);
    setScalar(a, "timespan.begin_of_the_begin", "from a");
    setScalar(b, "timespan.end_of_the_end", "from b");
    addReference(a, "part[Production].carried_out_by", "https://example.org/a");
    addReference(b, "part[Production].carried_out_by", "https://example.org/b");
    syncDocs([a, b]);
    expect(canonicalJson(project(a))).toBe(canonicalJson(project(b)));
    const span = project(a).timespan as JsonObject;
    expect(span.type).toBe("TimeSpan");
    expect(Object.hasOwn(span, "begin_of_the_begin")).not.toBe(
      Object.hasOwn(span, "end_of_the_end"),
    );
    const parts = project(a).part as JsonObject[];
    expect(parts.map((p) => (p.carried_out_by as JsonObject[])[0]?.id).sort()).toEqual([
      "https://example.org/a",
      "https://example.org/b",
    ]);
    expectRejected(a, "LAE-PROJ-007", () =>
      addReference(a, "part[Production].carried_out_by", "https://example.org/c"),
    );
  });
});

describe("removeEntry and moveEntry", () => {
  it("removes one entry and nothing else", () => {
    const doc = hydrate(
      record({ identified_by: names("a", "b"), classified_as: [{ id: AAT + "1" }] }),
    );
    const [first, second] = keys(doc, "identified_by") as [string, string];
    expect(removeEntry(doc, "identified_by[Name]", first)).toBe(true);
    expect(removeEntry(doc, "identified_by", first)).toBe(false);
    expect(contents(doc)).toEqual(["b"]);
    expect(getEntry(doc, "identified_by", second).get(ORDER_KEY)).toBe(1);
    expect(removeEntry(doc, "identified_by", second)).toBe(true);
    expect(Object.hasOwn(project(doc), "identified_by")).toBe(false);
    expect(removeEntry(doc, "produced_by.identified_by", second)).toBe(false);
    expectRejected(doc, "LAE-PROJ-008", () => removeEntry(doc, "identified_by", "classified_as"));
    expectRejected(doc, "LAE-PROJ-008", () =>
      removeEntry(doc, "identified_by", entryKey("classified_as", AAT + "1")),
    );
    expect(project(doc).classified_as).toEqual([{ id: AAT + "1" }]);
  });

  it("removal wins over a concurrent edit inside the entry", () => {
    const a = hydrate(record({ identified_by: names("a", "b") }));
    const b = fork(a);
    const first = keys(a, "identified_by")[0] as string;
    removeEntry(a, "identified_by", first);
    const inside = getEntry(b, "identified_by", first);
    setScalar(b, "content", "edited", { base: inside });
    addNode(b, "part[Name]", { content: "new" }, { base: inside });
    syncDocs([a, b]);
    expect(contents(a)).toEqual(["b"]);
    expect(contents(b)).toEqual(["b"]);
  });

  it("moves an entry, and writes nothing when it is already in place", () => {
    const doc = hydrate(record({ identified_by: names("a", "b", "c", "d") }));
    const [a, b, c, d] = keys(doc, "identified_by") as [string, string, string, string];
    moveEntry(doc, "identified_by", d, a);
    expect(contents(doc)).toEqual(["d", "a", "b", "c"]);
    moveEntry(doc, "identified_by", d, null);
    expect(contents(doc)).toEqual(["a", "b", "c", "d"]);
    moveEntry(doc, "identified_by[Name]", a, c);
    expect(contents(doc)).toEqual(["b", "a", "c", "d"]);
    const orders = Object.fromEntries(
      listEntries(doc, "identified_by").map(([k, e]) => [k, e.get(ORDER_KEY)]),
    );
    expect(orders).toEqual({ [b]: 1, [a]: 1.5, [c]: 2, [d]: 3 });
    for (const [key, before] of [
      [a, c],
      [d, null],
      [b, b],
      [b, a],
    ] as const) {
      const state = Y.encodeStateAsUpdate(doc);
      moveEntry(doc, "identified_by", key, before);
      expect(Y.encodeStateAsUpdate(doc)).toEqual(state);
    }
    const missing = entryKey("identified_by", "missing");
    expectRejected(doc, "LAE-PROJ-008", () => moveEntry(doc, "identified_by", missing, a));
    expectRejected(doc, "LAE-PROJ-008", () => moveEntry(doc, "identified_by", a, missing));
    expectRejected(doc, "LAE-PROJ-008", () => moveEntry(doc, "classified_as", a, null));
    expectRejected(doc, "LAE-PROJ-008", () => moveEntry(doc, "produced_by.part", a, null));
  });

  it("a move writes one _order unless it must rebalance, and a rebalance writes only changes", () => {
    const doc = hydrate(record({ identified_by: names("a", "b", "c") }));
    const [a, b, c] = keys(doc, "identified_by") as [string, string, string];
    getEntry(doc, "identified_by", b).set(ORDER_KEY, 0); // a tie, as concurrent inserts leave it
    const [low, high] = [a, b].sort() as [string, string];
    expect(keys(doc, "identified_by")).toEqual([low, high, c]);
    const written: string[] = [];
    recordMap(doc).observeDeep((events) => {
      for (const event of events) {
        written.push(`${String(event.path[0])}:${[...event.keys.keys()].join(",")}`);
      }
    });
    moveEntry(doc, "identified_by", c, high);
    expect(keys(doc, "identified_by")).toEqual([low, c, high]);
    expect(listEntries(doc, "identified_by").map(([, e]) => e.get(ORDER_KEY))).toEqual([0, 0.5, 1]);
    // Rebalance renumbered `high` (0 -> 1) and `c` (2 -> 2: unchanged, not written); then `c`
    // took the midpoint. `low` already held 0 and was not written.
    expect(written.sort()).toEqual([`${c}:_order`, `${high}:_order`].sort());
  });

  it("the 53rd insert before the last entry is the last without a rebalance", () => {
    const doc = hydrate(record({ identified_by: names("first", "last") }));
    const last = keys(doc, "identified_by")[1] as string;
    for (let i = 1; i < 54; i += 1) {
      addNode(doc, "identified_by[Name]", { content: String(i) }, { beforeKey: last });
    }
    let entries = listEntries(doc, "identified_by");
    expect(entries[entries.length - 2]?.[1].get(ORDER_KEY)).toBe(1 - 2 ** -53);
    expect(entries[entries.length - 1]?.[1].get(ORDER_KEY)).toBe(1);
    addNode(doc, "identified_by[Name]", { content: "54" }, { beforeKey: last });
    entries = listEntries(doc, "identified_by");
    expect(entries.map(([, e]) => e.get(ORDER_KEY))).toEqual([
      ...Array.from({ length: 54 }, (_, i) => i),
      53.5,
      54,
    ]);
    expect(contents(doc)).toEqual([
      "first",
      ...Array.from({ length: 54 }, (_, i) => String(i + 1)),
      "last",
    ]);
  });
});

describe("setScalar", () => {
  it("stores values exactly and rejects what cannot be stored", () => {
    const doc = hydrate(record({ dimension: [{ type: "Dimension", value: 1 }] }));
    for (const value of [5, -7, 33.5, 2 ** 60, true, false, null, "text", "", 0, 1e21]) {
      setScalar(doc, "dimension.value", value);
      expect(getPath(doc, "dimension.value")).toEqual([value]);
      expect(((project(doc).dimension as JsonObject[])[0] as JsonObject).value).toBe(value);
    }
    for (const [bad, expected] of [
      [Infinity, "LAE-PROJ-002"],
      ["\ud800", "LAE-PROJ-003"],
      [{ a: 1 }, "LAE-PROJ-007"],
      [[1], "LAE-PROJ-007"],
      [undefined, "LAE-PROJ-005"],
    ] as const) {
      expectRejected(doc, expected, () =>
        setScalar(doc, "dimension.value", bad as unknown as string),
      );
    }
  });

  it("makes content a Text only on the three types", () => {
    const doc = hydrate(
      record({
        identified_by: [{ type: "Identifier" }],
        referred_to_by: [{ type: "LinguisticObject", content: "old" }],
        classified_as: [{ id: AAT + "1", type: "Type" }],
      }),
    );
    setScalar(doc, "identified_by[Identifier].content", "INV 1");
    setScalar(doc, "classified_as.content", "plain");
    setScalar(doc, "content", "root");
    setScalar(doc, "identified_by[Name].content", "created");
    expect(getPath(doc, "identified_by[Identifier].content")[0]).toBeInstanceOf(Y.Text);
    expect(getPath(doc, "identified_by[Name].content")[0]).toBeInstanceOf(Y.Text);
    expect(getPath(doc, "classified_as.content")).toEqual(["plain"]);
    expect(getPath(doc, "content")).toEqual(["root"]);
    const text = getPath(doc, "referred_to_by.content")[0] as Y.Text;
    setScalar(doc, "referred_to_by.content", "new");
    expect(text.toString()).toBe("new");
    setScalar(doc, "referred_to_by.content", 5);
    expect(getPath(doc, "referred_to_by.content")).toEqual([5]);
    setScalar(doc, "referred_to_by.content", "again");
    expect(getPath(doc, "referred_to_by.content")[0]).toBeInstanceOf(Y.Text);
    const again = hydrate(project(doc));
    expect(canonicalJson(project(again))).toBe(canonicalJson(project(doc)));
  });

  it("on a Text keeps concurrent edits elsewhere, and writes nothing when unchanged", () => {
    const a = hydrate(record({ identified_by: names("The quick brown fox 🦊 jumps") }));
    const b = fork(a);
    setScalar(a, "identified_by.content", "A quick brown fox 🦊 jumps");
    setScalar(b, "identified_by.content", "The quick brown fox 🦊 leaps!");
    syncDocs([a, b]);
    expect(contents(a)).toEqual(["A quick brown fox 🦊 leaps!"]);
    expect(contents(b)).toEqual(["A quick brown fox 🦊 leaps!"]);
    const state = Y.encodeStateAsUpdate(a);
    setScalar(a, "identified_by.content", "A quick brown fox 🦊 leaps!");
    expect(Y.encodeStateAsUpdate(a)).toEqual(state);
  });

  it("reaches the new string on every replica", () => {
    const words = ["", "a", "ab", "😀b", "𝒳😀", "é", "é", "abc😀𝒳"];
    for (const start of words) {
      for (const first of words) {
        const second = words[(words.indexOf(first) + 3) % words.length] as string;
        const a = hydrate(record({ identified_by: names(start) }));
        const b = fork(a);
        setScalar(a, "identified_by.content", first);
        expect(contents(a)).toEqual([first]);
        setScalar(b, "identified_by.content", second);
        expect(contents(b)).toEqual([second]);
        syncDocs([a, b]);
        expect(contents(a)).toEqual(contents(b));
        setScalar(a, "identified_by.content", start);
        syncDocs([a, b]);
        expect(contents(b)).toEqual([start]);
      }
    }
  });

  it("rejections", () => {
    const doc = hydrate(
      record({
        identified_by: names("a", "b"),
        timespan: { type: "TimeSpan" },
        current_location: { id: "https://example.org/place" },
        x_scalar: "s",
      }),
    );
    for (const path of [
      "identified_by",
      "identified_by.content",
      "identified_by[Name].content",
      "timespan",
      "current_location",
      "timespan[TimeSpan]",
      "x_scalar.inner",
      "shows.content",
      "timespan[Period].begin_of_the_begin",
    ]) {
      expectRejected(doc, "LAE-PROJ-007", () => setScalar(doc, path, "x"));
    }
    expectRejected(doc, "LAE-PROJ-006", () => setScalar(doc, "x_scalar[Name].content", "x"));
  });

  it("creates missing intermediates with the model class or the filter", () => {
    const doc = hydrate(record());
    for (const [key, cls] of SINGLE_VALUED_CLASSES) {
      setScalar(doc, `${key}.x_note`, "v");
      const node = project(doc)[key] as JsonObject;
      expect(node.type).toBe(cls);
      expect(isInternalNodeIri(node.id, URI)).toBe(true);
      expect((getPath(doc, key)[0] as YMap).has(ORDER_KEY)).toBe(false);
    }
    setScalar(doc, "created_by.part[Creation].timespan.begin_of_the_begin", "1500");
    setScalar(doc, "dissolved_by.timespan[Period].x_note", "filter wins");
    const projected = project(doc);
    const created = projected.created_by as JsonObject;
    const [part] = created.part as JsonObject[];
    expect(part?.type).toBe("Creation");
    expect((part?.timespan as JsonObject).type).toBe("TimeSpan");
    expect(((projected.dissolved_by as JsonObject).timespan as JsonObject).type).toBe("Period");
    expect(created.x_note).toBe("v");
    expect(canonicalJson(stripInternalIris(projected))).not.toContain("/node/");
  });
});

describe("setObject and removeValue", () => {
  it("replace and remove single values", () => {
    const doc = hydrate(record({ identified_by: names("a"), x_scalar: 1 }));
    const span = { type: "TimeSpan", identified_by: names("about 1500") };
    const spanId = setObject(doc, "produced_by.timespan", span);
    expect(isInternalNodeIri(spanId, URI)).toBe(true);
    expect(span).toEqual({ type: "TimeSpan", identified_by: names("about 1500") });
    expect(contents(doc, "produced_by.timespan.identified_by.content")).toEqual(["about 1500"]);
    expect(setObject(doc, "produced_by.timespan[TimeSpan]", { end_of_the_end: "1" })).not.toBe(
      spanId,
    );
    const produced = () => project(doc).produced_by as JsonObject;
    expect(Object.hasOwn(produced().timespan as JsonObject, "identified_by")).toBe(false);
    const place = { id: "https://example.org/place", type: "Place", _label: "x" };
    expect(setObject(doc, "current_location", place)).toBe(place.id);
    expect(setObject(doc, "x_scalar", { id: "https://example.org/over-a-scalar" })).toBe(
      "https://example.org/over-a-scalar",
    );
    expect(project(doc).current_location).toEqual(place);
    expect((getPath(doc, "current_location")[0] as YMap).has(ORDER_KEY)).toBe(false);
    for (const [path, obj] of [
      ["identified_by", { type: "Name" }],
      ["x_new", { no: "type" }],
      ["x_new", { id: 5, type: "T" }],
      ["x_new[Name]", { type: "Type" }],
      ["x_new", "scalar"],
    ] as const) {
      expectRejected(doc, "LAE-PROJ-007", () => setObject(doc, path, obj as unknown as JsonObject));
    }
    expectRejected(doc, "LAE-PROJ-001", () => setObject(doc, "x_new", { type: "T", _order: 0 }));

    expect(removeValue(doc, "produced_by.timespan[Period]")).toBe(false);
    expect(removeValue(doc, "produced_by.timespan[TimeSpan]")).toBe(true);
    expect(removeValue(doc, "produced_by.timespan")).toBe(false);
    expect(removeValue(doc, "x_scalar")).toBe(true);
    expect(removeValue(doc, "created_by.timespan")).toBe(false);
    expect(Object.hasOwn(project(doc), "created_by")).toBe(false);
    expectRejected(doc, "LAE-PROJ-007", () => removeValue(doc, "identified_by"));
    expect(produced()).toEqual({ id: produced().id, type: "Production" });
  });

  it("concurrent setObject keeps one whole value", () => {
    const a = hydrate(record({ produced_by: { type: "Production" } }));
    const b = fork(a);
    setObject(a, "produced_by.timespan", { type: "TimeSpan", begin_of_the_begin: "a" });
    setObject(b, "produced_by.timespan", { type: "TimeSpan", end_of_the_end: "b" });
    syncDocs([a, b]);
    expect(canonicalJson(project(a))).toBe(canonicalJson(project(b)));
    const span = (project(a).produced_by as JsonObject).timespan as JsonObject;
    expect(["id,type,begin_of_the_begin", "id,type,end_of_the_end"]).toContain(
      Object.keys(span)
        .sort((x, y) =>
          x === "id" ? -1 : y === "id" ? 1 : x === "type" ? -1 : y === "type" ? 1 : 0,
        )
        .join(","),
    );
  });
});

describe("spliceText (R19)", () => {
  it("counts code points", () => {
    const doc = hydrate(record({ identified_by: names("a😀b𝒳c") }));
    const path = "identified_by.content";
    spliceText(doc, path, 2, 0, "é");
    expect(contents(doc)).toEqual(["a😀éb𝒳c"]);
    spliceText(doc, path, 1, 2, "");
    expect(contents(doc)).toEqual(["ab𝒳c"]);
    spliceText(doc, path, 2, 1, "🎨🎨");
    expect(contents(doc)).toEqual(["ab🎨🎨c"]);
    spliceText(doc, path, 5, 0, "!");
    spliceText(doc, path, 0, 0, "¡");
    expect(contents(doc)).toEqual(["¡ab🎨🎨c!"]);
    const state = Y.encodeStateAsUpdate(doc);
    spliceText(doc, path, 3, 0, "");
    expect(Y.encodeStateAsUpdate(doc)).toEqual(state);
    for (const [index, deleteCount] of [
      [8, 0],
      [7, 1],
      [-1, 0],
      [0, -1],
      [0, 8],
    ] as const) {
      expectRejected(doc, "LAE-PROJ-008", () => spliceText(doc, path, index, deleteCount, "x"));
    }
    expectRejected(doc, "LAE-PROJ-003", () => spliceText(doc, path, 0, 0, "\udfff"));
    for (const bad of ["identified_by", "identified_by[Name]", "x_absent", "produced_by.content"]) {
      expectRejected(doc, "LAE-PROJ-007", () => spliceText(doc, bad, 0, 0, "x"));
    }
  });

  it("takes integers and a string", () => {
    const doc = hydrate(record({ identified_by: names("abc") }));
    const path = "identified_by.content";
    const calls: (() => unknown)[] = [
      () => spliceText(doc, path, 0, 1, 5 as unknown as string),
      () => spliceText(doc, path, 0, 1, null as unknown as string),
      () => spliceText(doc, path, 0.5, 1, "x"),
      () => spliceText(doc, path, 0, 1.5, "x"),
      () => spliceText(doc, path, true as unknown as number, 0, "x"),
      () => spliceText(doc, path, 0, false as unknown as number, "x"),
    ];
    for (const call of calls) {
      expectRejected(doc, "LAE-PROJ-007", call);
    }
    expectRejected(doc, "LAE-PROJ-003", () => spliceText(doc, path, 0, 1, "\ud800"));
    expect(contents(doc)).toEqual(["abc"]);
  });
});

describe("transactions and write paths (R20, R25)", () => {
  it("every operation is one transaction and takes the caller's origin", () => {
    const doc = hydrate(record({ identified_by: names("a", "b") }));
    const origins: unknown[] = [];
    doc.on("afterTransaction", (transaction: Y.Transaction) => {
      origins.push(transaction.origin);
    });
    setScalar(doc, "produced_by.part[Production].timespan.begin_of_the_begin", "1500");
    addNode(doc, "produced_by.part[Production].identified_by[Name]", { content: "x" });
    const [first, second] = keys(doc, "identified_by") as [string, string];
    moveEntry(doc, "identified_by", second, first, { origin: "local" });
    removeEntry(doc, "identified_by", first, { origin: "local" });
    expect(origins).toEqual([null, null, "local", "local"]);
    doc.transact(() => {
      addReference(doc, "classified_as", AAT + "1", { type: "Type", origin: "ignored" });
      setScalar(doc, "identified_by.content", "B");
    }, "agent-turn-1");
    expect(origins).toEqual([null, null, "local", "local", "agent-turn-1"]);
  });

  it("sortedEntries ignores other properties and escaped keys", () => {
    const doc = hydrate(record({ identified_by: names("a"), classified_as: [{ id: AAT + "1" }] }));
    const root = recordMap(doc);
    expect(sortedEntries(root, "identified_by")).toHaveLength(1);
    expect(sortedEntries(root, "identified")).toEqual([]);
    expect(sortedEntries(root, "type")).toEqual([]);
  });

  it.each(["id", "type", "_label"])("a write path never names %s (LAE-PROJ-007)", (key) => {
    const doc = hydrate(record({ identified_by: names("a"), timespan: { type: "TimeSpan" } }));
    const [first] = keys(doc, "identified_by") as [string];
    const entry = getEntry(doc, "identified_by", first);
    const ref = { id: AAT + "1", type: "Type" };
    const calls: (() => unknown)[] = [
      () => setScalar(doc, key, "x"),
      () => setScalar(doc, key, "x", { base: entry }),
      () => setScalar(doc, `timespan.${key}`, "x"),
      () => setScalar(doc, `produced_by.${key}`, "x", { mint: never }),
      () => setScalar(doc, `produced_by.${key}[Type].x_note`, "x", { mint: never }),
      () => setObject(doc, key, ref),
      () => setObject(doc, `produced_by.timespan.${key}`, ref, { mint: never }),
      () => removeValue(doc, key),
      () => removeValue(doc, `timespan.${key}`),
      () => spliceText(doc, key, 0, 0, "x", { base: entry }),
      () => addNode(doc, key, { type: "Name" }, { mint: never }),
      () => addNode(doc, `identified_by[Identifier].${key}`, { type: "T" }, { mint: never }),
      () => addReference(doc, `identified_by[Identifier].${key}`, AAT + "1", { mint: never }),
      () => addReference(doc, `produced_by.${key}[Type].technique`, AAT + "1", { mint: never }),
      () => removeEntry(doc, key, entryKey(key, "x")),
      () => moveEntry(doc, key, entryKey(key, "x"), null),
    ];
    for (const call of calls) {
      expectRejected(doc, "LAE-PROJ-007", call);
    }
    const projected = project(doc);
    expect(projected.id).toBe(URI);
    expect(Object.hasOwn(projected, "produced_by")).toBe(false);
    expect((projected.identified_by as JsonObject[]).map((i) => i.type)).toEqual(["Name"]);
  });

  it("reading id and type through a path is not restricted", () => {
    const doc = hydrate(record({ timespan: { type: "TimeSpan" } }));
    expect(getPath(doc, "id")).toEqual([URI]);
    expect(getPath(doc, "timespan.type")).toEqual(["TimeSpan"]);
  });
});
