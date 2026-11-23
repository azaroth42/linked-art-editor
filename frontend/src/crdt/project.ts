/**
 * Y.Doc to JSON-LD: projection (R11, R13). TypeScript mirror of
 * `backend/src/lae/projection/project.py`.
 *
 * Output objects are built with {@link put} (`Object.defineProperty`), so a JSON key such as
 * `__proto__` is stored as data and never sets a prototype; groupings use `Map`s.
 */
import * as Y from "yjs";

import { isInternalNodeIri, resolveRecordUri } from "./hydrate.ts";
import {
  compareCodePoints,
  decodePlain,
  isObject,
  type JsonObject,
  OPAQUE_KEYS,
  ORDER_KEY,
  own,
  parseKey,
  PROJ_BAD_STATE,
  ProjectionError,
  put,
  RESERVED_KEYS,
  ROOT_MAP,
  VALUE_KEY,
} from "./rules.ts";

/** The root `record` map of a document (R1). */
export function recordMap(doc: Y.Doc): Y.Map<unknown> {
  return doc.getMap<unknown>(ROOT_MAP);
}

/** The numeric `_order` of an entry map, or `null` if it has none. */
export function entryOrder(entry: unknown): number | null {
  if (!(entry instanceof Y.Map) || !entry.has(ORDER_KEY)) {
    return null;
  }
  const order: unknown = entry.get(ORDER_KEY);
  return typeof order === "number" && !Number.isNaN(order) ? order : null;
}

function compareNumbers(a: number, b: number): number {
  if (a < b) {
    return -1;
  }
  return a > b ? 1 : 0;
}

/** R13: by `_order`, then by the whole entry key in Unicode code-point order. */
export function sortEntries(entries: [string, unknown][]): [string, Y.Map<unknown>][] {
  const keyed = entries.map(([key, entry]) => {
    const order = entryOrder(entry);
    if (order === null) {
      throw new ProjectionError(PROJ_BAD_STATE, `entry ${key} has no numeric _order`);
    }
    return { key, entry: entry as Y.Map<unknown>, order };
  });
  keyed.sort((a, b) => compareNumbers(a.order, b.order) || compareCodePoints(a.key, b.key));
  return keyed.map(({ key, entry }) => [key, entry]);
}

/** The entries of property `prop` of an object map as `[entry key, entry]`, in R13 order. */
export function sortedEntries(parent: Y.Map<unknown>, prop: string): [string, Y.Map<unknown>][] {
  const found: [string, unknown][] = [];
  for (const [key, value] of parent.entries()) {
    const [keyProp, itemKey] = parseKey(key);
    if (itemKey !== null && keyProp === prop) {
      found.push([key, value]);
    }
  }
  return sortEntries(found);
}

/** Project the `record` map back to JSON-LD (R11); internal node IRIs are kept. */
export function project(doc: Y.Doc): JsonObject {
  return projectObject(recordMap(doc));
}

/** Project one object map (the root, a node or a reference) to a JSON object. */
export function projectObject(obj: Y.Map<unknown>): JsonObject {
  const out: JsonObject = {};
  const arrays = new Map<string, [string, unknown][]>();
  for (const [key, value] of obj.entries()) {
    const [prop, itemKey] = parseKey(key);
    if (itemKey !== null) {
      const entries = arrays.get(prop) ?? [];
      entries.push([key, value]);
      arrays.set(prop, entries);
    } else if (!RESERVED_KEYS.has(key)) {
      put(out, prop, projectValue(value));
    }
  }
  // Entries win over a single value stored for the same property (R11).
  for (const [prop, entries] of arrays) {
    put(
      out,
      prop,
      sortEntries(entries).map(([, entry]) => projectEntry(entry)),
    );
  }
  return out;
}

/** Project a value stored under a property key: a `Text`, an object map or a plain value. */
export function projectValue(value: unknown): unknown {
  if (value instanceof Y.Text) {
    return value.toString();
  }
  if (value instanceof Y.Map) {
    return projectObject(value as Y.Map<unknown>);
  }
  return decodePlain(value);
}

/** Project one array entry: its decoded `_value` if it has one, else an object. */
export function projectEntry(entry: Y.Map<unknown>): unknown {
  if (entry.has(VALUE_KEY)) {
    return decodePlain(entry.get(VALUE_KEY));
  }
  return projectObject(entry);
}

/**
 * A copy without any `id` that is an internal node IRI of this record (R15): the public form.
 * `recordUri` defaults to the root `id`, which is never removed; `@context` is not touched.
 */
export function stripInternalIris(jsonld: JsonObject, recordUri?: string): JsonObject {
  const uri = resolveRecordUri(recordUri, own(jsonld, "id"));
  const out: JsonObject = {};
  for (const [key, value] of Object.entries(jsonld)) {
    put(out, key, OPAQUE_KEYS.has(key) ? value : strip(value, uri));
  }
  return out;
}

function strip(value: unknown, uri: string): unknown {
  if (Array.isArray(value)) {
    return value.map((item: unknown) => strip(item, uri));
  }
  if (!isObject(value)) {
    return value;
  }
  const out: JsonObject = {};
  for (const [key, item] of Object.entries(value)) {
    if (!(key === "id" && isInternalNodeIri(item, uri))) {
      put(out, key, OPAQUE_KEYS.has(key) ? item : strip(item, uri));
    }
  }
  return out;
}
