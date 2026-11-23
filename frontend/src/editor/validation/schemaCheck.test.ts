/**
 * The browser's schema check against the server's verdicts: every hand-made case and every
 * corpus record of `backend/tests/validation/data/schema_browser_cases.json` (generated from
 * `lae.validation.schema.validate_schema`) must give exactly the same `code`, `path`, `missing`
 * and `extra`, with the schema files `GET /api/profiles/schema/{class}` serves.
 */
import { describe, expect, it } from "vitest";

import type { Diagnostic } from "../../api/hooks/publishing.ts";
import { canonicalJson, type JsonObject } from "../../crdt/rules.ts";
import {
  checkSchema,
  compileSchemaCheck,
  isDateTime,
  isUri,
  prepareForSchema,
  type SchemaCheck,
} from "./schemaCheck.ts";
import { servedSchema } from "./testing.ts";

const CORPUS = import.meta.glob<JsonObject>("../../../../tests/corpus/*/*.json", {
  eager: true,
  import: "default",
});
const CASES = import.meta.glob<Fixture>(
  "../../../../backend/tests/validation/data/schema_browser_cases.json",
  { eager: true, import: "default" },
);

interface Expected {
  code: string;
  path: string;
  missing?: string[];
  extra?: string[];
}

interface Fixture {
  cases: { name: string; document: JsonObject; expected: Expected[] }[];
  corpus: Record<string, Expected[]>;
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

describe("checkSchema", () => {
  it.each(FIXTURE.cases.map((item) => [item.name, item] as const))(
    "%s: the server's verdict",
    (_name, item) => {
      const check = checkFor(String(item.document.type));
      expect(verdict(checkSchema(check, item.document))).toEqual(item.expected);
    },
  );

  it("agrees with the server on every corpus record it can check", () => {
    const disagreements: string[] = [];
    for (const [name, expected] of Object.entries(FIXTURE.corpus)) {
      const record = CORPUS[`../../../../tests/corpus/${name}`];
      if (record === undefined) {
        throw new Error(`corpus file ${name} is missing`);
      }
      const found = verdict(checkSchema(checkFor(String(record.type)), record));
      if (canonicalJson(found) !== canonicalJson(expected)) {
        disagreements.push(`${name}: ${JSON.stringify(found)} != ${JSON.stringify(expected)}`);
      }
    }
    expect(Object.keys(FIXTURE.corpus).length).toBeGreaterThan(100);
    expect(disagreements).toEqual([]);
  });

  it("gives the params the English messages interpolate", () => {
    const [item] = FIXTURE.cases.filter((c) => c.name === "missing root _label");
    const check = checkFor("HumanMadeObject");
    expect(checkSchema(check, item?.document ?? {})).toEqual([
      {
        code: "LAE-SCHEMA-required",
        path: "",
        severity: "violation",
        params: { missing: ["_label"] },
        message_en: "",
      },
    ]);
  });
});

describe("prepareForSchema", () => {
  it("removes internal node IRIs and repeated contexts, and nothing else", () => {
    const uri = "https://data.test/id/1";
    const node = `${uri}/node/01937a4e-0000-7000-8000-000000000001`;
    const context = "https://linked.art/ns/v1/linked-art.json";
    const record = {
      "@context": context,
      id: uri,
      type: "HumanMadeObject",
      identified_by: [{ id: node, type: "Name", content: "A", "@context": context }],
      made_of: [{ id: "http://vocab.getty.edu/aat/300015050", type: "Material" }],
    };
    expect(prepareForSchema(record)).toEqual({
      "@context": context,
      id: uri,
      type: "HumanMadeObject",
      identified_by: [{ type: "Name", content: "A" }],
      made_of: [{ id: "http://vocab.getty.edu/aat/300015050", type: "Material" }],
    });
    expect(record.identified_by[0]?.id).toBe(node);
  });
});

describe("formats", () => {
  it("accepts signed years and refuses impossible dates and times", () => {
    expect(isDateTime("-0479-01-01T00:00:00Z")).toBe(true);
    expect(isDateTime("2024-02-29T24:00:00+14:00")).toBe(true);
    expect(isDateTime("2023-02-29T00:00:00Z")).toBe(false);
    expect(isDateTime("1642-13-01T00:00:00Z")).toBe(false);
    expect(isDateTime("1642-01-01T24:00:01Z")).toBe(false);
    expect(isDateTime("1642-01-01")).toBe(false);
  });

  it("accepts what expansion can write as an IRI", () => {
    expect(isUri("http://example.org/a b")).toBe(true);
    expect(isUri("urn:uuid:1")).toBe(true);
    expect(isUri("not an iri")).toBe(false);
    expect(isUri("http://")).toBe(false);
  });
});
