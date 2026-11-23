/**
 * The JSON Schema check in the browser (Step 1.19): the draft is checked with Ajv against the
 * schema `GET /api/profiles/schema/{class}` serves, the one the server's first validation stage
 * (`lae.validation.schema`) uses, so most structural problems show while the user types.
 * `POST /api/records/{id}/draft/validate` stays the authority (SHACL, the rules, RDF and the
 * database facts run only there), and publish validates again.
 *
 * The server's preparation is mirrored so that the same document gives the same findings:
 *
 * - the files declare 2020-12 but are read as Draft 7 (`profiles/schema/SOURCE.md`);
 * - formats: only `date-time` (signed years) and `uri` are checked;
 * - before the check the `id` of every embedded node is removed: an internal node IRI
 *   (`strip_internal_iris`) and the foreign id of a node that expansion embeds
 *   (`lae.rdf.strip_embedded_ids`, ADR-0028), and a nested `@context` that repeats the root's is
 *   dropped (`strip_nested_contexts`); only keys are removed, so every pointer stays valid;
 * - one diagnostic per keyword the server reports (`REPORTED_KEYWORDS`): `required` names every
 *   missing property at once, and the errors inside the branches of an `anyOf` / `oneOf` are
 *   folded into that one error, as `jsonschema` reports them;
 * - a `type` error on a root `equivalent` entry of the record's own class is not reported.
 *
 * Every finding is a violation with the code `LAE-SCHEMA-<keyword>` and the `params` the English
 * messages of `errors.json` use (`missing`, `extra`, `format`, `validator_value`).
 */
import { Ajv, type ErrorObject, type ValidateFunction } from "ajv";

import type { Diagnostic, RecordSchema } from "../../api/hooks/publishing.ts";
import { isInternalNodeIri } from "../../crdt/hydrate.ts";
import { canonicalJson, compareCodePoints, isObject, type JsonObject } from "../../crdt/rules.ts";

/** The keywords the server can name in a code (`lae.validation.schema.REPORTED_KEYWORDS`). */
export const REPORTED_KEYWORDS: ReadonlySet<string> = new Set([
  "additionalItems",
  "additionalProperties",
  "anyOf",
  "const",
  "contains",
  "dependencies",
  "enum",
  "exclusiveMaximum",
  "exclusiveMinimum",
  "format",
  "maxItems",
  "maxLength",
  "maxProperties",
  "maximum",
  "minItems",
  "minLength",
  "minProperties",
  "minimum",
  "multipleOf",
  "not",
  "oneOf",
  "pattern",
  "required",
  "type",
  "uniqueItems",
]);

/** Ajv's keyword for a `false` schema; the server's code is `LAE-SCHEMA-false`. */
const FALSE_SCHEMA = "false schema";

/** Classes Linked Art never embeds (`lae.rdf.expand.ENTITY_TYPES`). */
const ENTITY_TYPES: ReadonlySet<string> = new Set([
  "Type",
  "Language",
  "Material",
  "Currency",
  "MeasurementUnit",
  "Person",
  "Group",
  "Place",
  "HumanMadeObject",
  "Set",
]);

/** Classes that exist only inside a record (`lae.rdf.expand.EMBEDDED_TYPES`). */
const EMBEDDED_TYPES: ReadonlySet<string> = new Set([
  "Name",
  "Identifier",
  "TimeSpan",
  "Dimension",
  "MonetaryAmount",
  "AttributeAssignment",
  "Production",
  "Destruction",
  "Creation",
  "Birth",
  "Death",
  "Formation",
  "Dissolution",
  "Joining",
  "Leaving",
  "Acquisition",
  "Payment",
  "TransferOfCustody",
  "Move",
  "Encounter",
  "PartAddition",
  "PartRemoval",
  "Modification",
  "Transformation",
  "Addition",
  "Removal",
  "RightAcquisition",
  "Right",
  "DigitalService",
]);

const ID_KEYS = ["id", "@id"] as const;
const STUB_KEYS: ReadonlySet<string> = new Set([
  "id",
  "@id",
  "type",
  "@type",
  "_label",
  "@context",
]);
const CLASSIFIED_STUB_KEYS: ReadonlySet<string> = new Set([...STUB_KEYS, "classified_as"]);
const SCHEMA_REFERENCE_KEYS: ReadonlySet<string> = new Set([...CLASSIFIED_STUB_KEYS, "equivalent"]);

/** `lae.rdf.sanitize.VALID_DATETIME`, with the field ranges of `lae.validation.datetimes`. */
const DATETIME =
  /^(-?(?:[1-9]\d{4,}|\d{4}))-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-](\d\d):(\d\d))?$/;
const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/** True if `value` is an `xsd:dateTime` with an in-range date, time and zone (signed years). */
export function isDateTime(value: string): boolean {
  const match = DATETIME.exec(value);
  if (match === null) {
    return false;
  }
  const [year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0] = match
    .slice(1, 7)
    .map(Number);
  const fraction = Number(match[7] ?? 0);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  if (month < 1 || month > 12) {
    return false;
  }
  const days = (DAYS_IN_MONTH[month - 1] ?? 0) + (month === 2 && leap ? 1 : 0);
  if (day < 1 || day > days || minute > 59 || second > 59) {
    return false;
  }
  if (hour > 24 || (hour === 24 && (minute !== 0 || second !== 0 || fraction !== 0))) {
    return false;
  }
  if (match[9] !== undefined) {
    const zoneMinutes = Number(match[10]);
    return zoneMinutes <= 59 && Number(match[9]) * 60 + zoneMinutes <= 14 * 60;
  }
  return true;
}

/** Characters an IRI cannot hold; expansion percent-encodes them (`lae.rdf.repair_iri`). */
// eslint-disable-next-line no-control-regex
const UNWRITABLE = /[\u0000- <>"{}|^`\\]|\s/gu;
const LONE_SURROGATE = /\p{Cs}/u;
const ABSOLUTE_IRI = /^(?:https?:\/\/[^/?#]|(?!https?:)[A-Za-z][A-Za-z0-9+.-]*:.)/iu;

/** True if `value` can be written as an absolute IRI, percent-encoding where needed. */
export function isUri(value: string): boolean {
  return !LONE_SURROGATE.test(value) && ABSOLUTE_IRI.test(value.replace(UNWRITABLE, "%20"));
}

function types(obj: JsonObject): Set<string> {
  const found = new Set<string>();
  for (const key of ["type", "@type"]) {
    const value = obj[key];
    for (const item of Array.isArray(value) ? (value as unknown[]) : [value]) {
      if (typeof item === "string") {
        found.add(item);
      }
    }
  }
  return found;
}

function identifier(obj: JsonObject): string | null {
  for (const key of ID_KEYS) {
    const value = obj[key];
    if (typeof value === "string") {
      return value;
    }
  }
  return null;
}

function keysWithin(obj: JsonObject, allowed: ReadonlySet<string>): boolean {
  return Object.keys(obj).every((key) => allowed.has(key));
}

function intersects(a: Set<string>, b: ReadonlySet<string>): boolean {
  return [...a].some((item) => b.has(item));
}

/** `lae.rdf.expand._is_schema_embedded`: a node with a foreign id the schema has no place for. */
function isSchemaEmbedded(obj: JsonObject, recordUri: string): boolean {
  const id = identifier(obj);
  if (id === null || id === recordUri || isInternalNodeIri(id, recordUri)) {
    return false;
  }
  if (id.startsWith("_:") || keysWithin(obj, STUB_KEYS)) {
    return false;
  }
  const classes = types(obj);
  if (intersects(classes, ENTITY_TYPES)) {
    return false;
  }
  if (intersects(classes, EMBEDDED_TYPES)) {
    return true;
  }
  // `_is_embedded` asks for more than CLASSIFIED_STUB_KEYS; the schema allows `equivalent` too.
  return !keysWithin(obj, SCHEMA_REFERENCE_KEYS);
}

function isPinnedContext(value: unknown, root: unknown): boolean {
  return (
    typeof root === "string" &&
    (value === root || (Array.isArray(value) && value.length === 1 && value[0] === root))
  );
}

function prepare(value: unknown, recordUri: string, rootContext: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item: unknown) => prepare(item, recordUri, rootContext));
  }
  if (!isObject(value)) {
    return value;
  }
  const dropIds = isSchemaEmbedded(value, recordUri);
  const out: JsonObject = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === "@context") {
      if (!isPinnedContext(item, rootContext)) {
        out[key] = item;
      }
    } else if (key === "id" && isInternalNodeIri(item, recordUri)) {
      continue;
    } else if (!(dropIds && (key === "id" || key === "@id"))) {
      out[key] = prepare(item, recordUri, rootContext);
    }
  }
  return out;
}

/**
 * The document the schema sees (module docstring): the root is kept as it is, apart from what
 * its properties hold. Only keys are removed, so a pointer into the result is one into `jsonld`.
 */
export function prepareForSchema(jsonld: JsonObject, recordUri?: string): JsonObject {
  const uri = recordUri ?? (typeof jsonld.id === "string" ? jsonld.id : "");
  const out: JsonObject = {};
  for (const [key, value] of Object.entries(jsonld)) {
    out[key] = key === "@context" ? value : prepare(value, uri, jsonld["@context"]);
  }
  return out;
}

/** A compiled schema check for one record class. */
export interface SchemaCheck {
  validate: ValidateFunction;
  recordClass: string;
}

/** Compile the schema of `GET /api/profiles/schema/{class}`; throws if it cannot be compiled. */
export function compileSchemaCheck(schema: RecordSchema): SchemaCheck {
  const ajv = new Ajv({
    allErrors: true,
    strict: false,
    validateSchema: false,
    logger: false,
    formats: { "date-time": isDateTime, uri: isUri },
  });
  for (const contents of Object.values(schema.schemas)) {
    // Read as Draft 7 whatever the file declares (profiles/schema/SOURCE.md).
    const copy: Record<string, unknown> = { ...contents };
    delete copy.$schema;
    ajv.addSchema(copy);
  }
  const validate = ajv.getSchema(schema.entry);
  if (validate === undefined) {
    throw new Error(`schema ${schema.entry} is not among the served files`);
  }
  return { validate, recordClass: schema.class };
}

function isUnder(path: string, prefix: string): boolean {
  return path === prefix || path.startsWith(`${prefix}/`);
}

/**
 * The errors `jsonschema` would report: Ajv also reports every error inside the branches of a
 * failed `anyOf` / `oneOf` (just before that error, at or below its instance) and the keywords
 * that only pass errors on (`if`, `$ref`, `items`, ...).
 */
function reportedErrors(errors: readonly ErrorObject[]): ErrorObject[] {
  const kept: ErrorObject[] = [];
  for (const error of errors) {
    if (error.keyword === "anyOf" || error.keyword === "oneOf") {
      for (let last = kept.at(-1); last !== undefined; last = kept.at(-1)) {
        if (!isUnder(last.instancePath, error.instancePath)) {
          break;
        }
        kept.pop();
      }
    }
    kept.push(error);
  }
  return kept.filter(
    (error) => REPORTED_KEYWORDS.has(error.keyword) || error.keyword === FALSE_SCHEMA,
  );
}

function valueAt(document: unknown, pointer: string): unknown {
  let current = document;
  for (const token of pointer === "" ? [] : pointer.slice(1).split("/")) {
    const key = token.replaceAll("~1", "/").replaceAll("~0", "~");
    if (Array.isArray(current)) {
      current = (current as unknown[])[Number(key)];
    } else if (isObject(current)) {
      current = current[key];
    } else {
      return undefined;
    }
  }
  return current;
}

/** `lae.validation.schema._is_own_class_equivalent`. */
function isOwnClassEquivalent(error: ErrorObject, document: JsonObject): boolean {
  const parts = error.instancePath.split("/");
  return (
    (error.keyword === "const" || error.keyword === "enum") &&
    parts.length === 4 &&
    parts[1] === "equivalent" &&
    parts[3] === "type" &&
    typeof document.type === "string" &&
    valueAt(document, error.instancePath) === document.type
  );
}

function params(error: ErrorObject): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (error.keyword === "const") {
    out.validator_value = (error.params as { allowedValue: unknown }).allowedValue;
  } else if (error.keyword === "enum") {
    out.validator_value = (error.params as { allowedValues: unknown }).allowedValues;
  } else if (error.keyword === "format") {
    out.format = (error.params as { format: string }).format;
  } else if (error.keyword === "additionalProperties") {
    out.extra = [(error.params as { additionalProperty: string }).additionalProperty];
  } else if (error.keyword === "type") {
    out.validator_value = (error.params as { type: unknown }).type;
  } else if ("limit" in error.params) {
    out.validator_value = (error.params as { limit: unknown }).limit;
  } else if ("pattern" in error.params) {
    out.validator_value = (error.params as { pattern: unknown }).pattern;
  }
  return out;
}

function sortedUnique(values: string[]): string[] {
  return [...new Set(values)].sort();
}

/**
 * The JSON Schema violations of a draft, in document order (pointer, then code), as the server
 * would report them. `jsonld` is the projected draft with its internal node IRIs.
 */
export function checkSchema(check: SchemaCheck, jsonld: JsonObject): Diagnostic[] {
  const document = prepareForSchema(jsonld);
  if (check.validate(document)) {
    return [];
  }
  const found = new Map<string, Diagnostic>();
  for (const error of reportedErrors(check.validate.errors ?? [])) {
    if (isOwnClassEquivalent(error, jsonld)) {
      continue;
    }
    const keyword = error.keyword === FALSE_SCHEMA ? "false" : error.keyword;
    const code = `LAE-SCHEMA-${keyword}`;
    const path = error.instancePath;
    const key = canonicalJson([code, path]);
    const existing = found.get(key);
    if (keyword === "required" || keyword === "additionalProperties") {
      // One diagnostic per object, naming every missing (extra) property.
      const field = keyword === "required" ? "missing" : "extra";
      const name =
        keyword === "required"
          ? (error.params as { missingProperty: string }).missingProperty
          : (error.params as { additionalProperty: string }).additionalProperty;
      const before = (existing?.params[field] as string[] | undefined) ?? [];
      found.set(key, {
        code,
        path,
        severity: "violation",
        params: { [field]: sortedUnique([...before, name]) },
        message_en: "",
      });
    } else if (existing === undefined) {
      found.set(key, { code, path, severity: "violation", params: params(error), message_en: "" });
    }
  }
  return [...found.values()].sort((a, b) =>
    compareCodePoints(a.path, b.path) === 0
      ? compareCodePoints(a.code, b.code)
      : compareCodePoints(a.path, b.path),
  );
}
