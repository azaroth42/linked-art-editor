/**
 * The browser's derived `_label` against the server's (Step 1.19a, contract §5 tests 7–9):
 * every hand-made case and every corpus record of
 * `backend/tests/labels/data/derived_label_cases.json` (generated from `lae.labels.derive`)
 * must give the same label, the same derived document and the same schema verdict, under the
 * language each case names; and the derivation never touches the `Y.Doc`.
 */
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import type { Diagnostic } from "../api/hooks/publishing.ts";
import {
  checkSchema,
  compileSchemaCheck,
  type SchemaCheck,
} from "../editor/validation/schemaCheck.ts";
import { servedSchema } from "../editor/validation/testing.ts";
import { AAT_LANGUAGE_TAGS } from "../widgets/languages.ts";
import { deriveLabel, normalizeLabel, projectDraft, withDerivedLabel } from "./derivedLabel.ts";
import { hydrate } from "./hydrate.ts";
import { ISO_639_2_TO_1, primaryLanguageOf } from "./langtags.ts";
import { addNode, setScalar, spliceText } from "./paths.ts";
import { project } from "./project.ts";
import { canonicalJson, type JsonObject } from "./rules.ts";
import { syncDocs } from "./testing.ts";
import { createScopedUndoManager } from "./useRecordDoc.ts";

const CORPUS = import.meta.glob<JsonObject>("../../../tests/corpus/*/*.json", {
  eager: true,
  import: "default",
});
const CASES = import.meta.glob<Fixture>(
  "../../../backend/tests/labels/data/derived_label_cases.json",
  { eager: true, import: "default" },
);

interface Expected {
  code: string;
  path: string;
  missing?: string[];
  extra?: string[];
}

interface Fixture {
  cases: {
    name: string;
    language: string;
    document: JsonObject;
    label: string | null;
    derived: JsonObject;
    schema: Expected[];
  }[];
  corpus: Record<string, Record<string, string | null>>;
  languages: { aat: Record<string, string>; iso639_2: Record<string, string> };
}

const FIXTURE = Object.values(CASES)[0] as Fixture;

const checks = new Map<string, SchemaCheck>();
function checkFor(recordClass: string): SchemaCheck {
  let check = checks.get(recordClass);
  if (check === undefined) {
    check = compileSchemaCheck(servedSchema(recordClass));
    checks.set(recordClass, check);
  }
  return check;
}

function verdict(items: Diagnostic[]): Expected[] {
  return items.map(({ code, path, params }) => {
    const found: Expected = { code, path };
    for (const key of ["missing", "extra"] as const) {
      if (Array.isArray(params[key])) {
        found[key] = [...(params[key] as string[])].sort();
      }
    }
    return found;
  });
}

const URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const AAT = "http://vocab.getty.edu/aat/";
const PRIMARY = { id: `${AAT}300404670`, type: "Type" };
const ENGLISH = { id: `${AAT}300388277`, type: "Language" };
const FRENCH = { id: `${AAT}300388306`, type: "Language" };

describe("deriveLabel", () => {
  it("agrees with the server on every case and corpus record", () => {
    expect(FIXTURE.cases.length).toBeGreaterThan(50);
    for (const item of FIXTURE.cases) {
      const before = canonicalJson(item.document);
      const derived = withDerivedLabel(item.document, item.language);
      expect(deriveLabel(item.document, item.language), item.name).toBe(item.label);
      expect(canonicalJson(derived), item.name).toBe(canonicalJson(item.derived));
      // The argument is never modified, and it is returned as it is when nothing changes.
      expect(canonicalJson(item.document), item.name).toBe(before);
      expect(derived === item.document, item.name).toBe(
        canonicalJson(item.derived) === canonicalJson(item.document),
      );
      const found = verdict(checkSchema(checkFor(String(item.document.type)), derived));
      expect(found, item.name).toEqual(item.schema);
    }

    const languages = Object.keys(FIXTURE.corpus);
    expect(languages).toEqual(expect.arrayContaining(["en", "fr", "de"]));
    const disagreements: string[] = [];
    let compared = 0;
    for (const language of languages) {
      for (const [name, expected] of Object.entries(FIXTURE.corpus[language] ?? {})) {
        const record = CORPUS[`../../../tests/corpus/${name}`];
        if (record === undefined) {
          throw new Error(`corpus file ${name} is missing`);
        }
        const found = deriveLabel(record, language);
        if (found !== expected) {
          disagreements.push(`${language} ${name}: ${String(found)} != ${String(expected)}`);
        }
        compared += 1;
      }
    }
    expect(compared).toBeGreaterThan(600);
    expect(disagreements).toEqual([]);
  });

  it("has the server's language tables", () => {
    expect(Object.fromEntries(AAT_LANGUAGE_TAGS)).toEqual(FIXTURE.languages.aat);
    expect(Object.fromEntries(ISO_639_2_TO_1)).toEqual(FIXTURE.languages.iso639_2);
  });

  it("reads a language entry as the server does", () => {
    expect(primaryLanguageOf({ notation: " fre " })).toBe("fr");
    expect(primaryLanguageOf({ notation: "en-GB", id: `${AAT}300388306` })).toBe("en");
    expect(primaryLanguageOf({ notation: "english", id: `${AAT}300388306` })).toBe("english");
    expect(primaryLanguageOf({ notation: "not a tag", id: `aat:300388306` })).toBe("fr");
    expect(primaryLanguageOf({ id: "https://vocab.getty.edu/aat/300388344" })).toBe("de");
    expect(primaryLanguageOf({ id: `${AAT}３００３８８３０６` })).toBe("und");
    expect(primaryLanguageOf("fr")).toBe("und");
    expect(primaryLanguageOf(null)).toBe("und");
  });

  it("normalizes over Python's whitespace, not JavaScript's", () => {
    expect(normalizeLabel("\u001c A\u0085\u0085B 　")).toBe("A B");
    expect(normalizeLabel("﻿A​B")).toBe("﻿A​B");
  });
});

describe("projectDraft", () => {
  it("projectDraft fires no update and leaves one undo item per title edit", () => {
    const doc = hydrate({
      id: URI,
      type: "HumanMadeObject",
      _label: "Stale",
      identified_by: [{ type: "Name", content: "Night Scene", classified_as: [PRIMARY] }],
    });
    const origin = { client: "local" };
    const undo = createScopedUndoManager(doc, origin);
    const updates: Uint8Array[] = [];
    doc.on("update", (update: Uint8Array) => updates.push(update));
    const state = Y.encodeStateVector(doc);

    expect(projectDraft(doc, "en")._label).toBe("Night Scene");
    expect(updates).toEqual([]);
    expect(Y.encodeStateVector(doc)).toEqual(state);
    // The Doc still holds its own stale label: nothing was written into it.
    expect(project(doc)._label).toBe("Stale");

    spliceText(doc, "identified_by.content", 0, 0, "A ", { origin });
    expect(updates).toHaveLength(1);
    expect(projectDraft(doc, "en")._label).toBe("A Night Scene");
    expect(projectDraft(doc, "fr")._label).toBe("A Night Scene");
    expect(updates).toHaveLength(1);
    expect(undo.undoStack).toHaveLength(1);

    undo.undo();
    expect(projectDraft(doc, "en")._label).toBe("Night Scene");
    expect(undo.undoStack).toHaveLength(0);
    expect(project(doc)._label).toBe("Stale");
  });

  it("two replicas editing the name concurrently converge on one label", () => {
    const source = hydrate({
      id: URI,
      type: "HumanMadeObject",
      identified_by: [
        { type: "Name", content: "Title", classified_as: [PRIMARY], language: [ENGLISH] },
      ],
    });
    const update = Y.encodeStateAsUpdate(source);
    const results: string[] = [];
    for (const order of [
      ["a", "b"],
      ["b", "a"],
    ] as const) {
      const docs = { a: new Y.Doc(), b: new Y.Doc() };
      docs.a.clientID = 101;
      docs.b.clientID = 202;
      Y.applyUpdate(docs.a, update);
      Y.applyUpdate(docs.b, update);
      // a edits the English title; b edits it too and adds a French primary name.
      spliceText(docs.a, "identified_by.content", 5, 0, " by A");
      spliceText(docs.b, "identified_by.content", 0, 0, "B: ");
      addNode(
        docs.b,
        "identified_by[Name]",
        { content: "Titre", classified_as: [PRIMARY], language: [FRENCH] },
        { mint: () => `${URI}/node/0192f0c1-7b1e-7000-8000-000000000009` },
      );
      const [first, second] = order.map((name) => docs[name]);
      // Deliver one way first, then sync everything.
      Y.applyUpdate(second as Y.Doc, Y.encodeStateAsUpdate(first as Y.Doc));
      syncDocs([docs.a, docs.b]);
      for (const language of ["en", "fr"]) {
        expect(canonicalJson(projectDraft(docs.a, language))).toBe(
          canonicalJson(projectDraft(docs.b, language)),
        );
      }
      results.push(canonicalJson(projectDraft(docs.a, "en")));
      expect(projectDraft(docs.a, "fr")._label).toBe("Titre");
    }
    expect(results[0]).toBe(results[1]);
    expect(JSON.parse(results[0] as string)._label).toBe("B: Title by A");
  });
});

it("the editor's write path refuses _label", () => {
  const doc = hydrate({ id: URI, type: "HumanMadeObject", _label: "Kept" });
  expect(() => setScalar(doc, "_label", "x")).toThrow(/LAE-PROJ-007|cannot name _label/);
  expect(project(doc)._label).toBe("Kept");
});
