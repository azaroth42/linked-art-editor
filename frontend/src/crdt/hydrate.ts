/**
 * JSON-LD to Y types: internal node IRIs and the object maps of R5–R8.
 *
 * TypeScript mirror of `backend/src/lae/projection/hydrate.py`. A record is hydrated once per
 * draft epoch, **by the server**; a browser never hydrates a record for itself (R2), it applies
 * the server's state. The editor needs the per-object functions here to add nodes (`addNode`,
 * `setObject` in `paths.ts`); {@link hydrate} itself exists for tests and tools.
 */
import { v7 as uuidv7 } from "uuid";
import * as Y from "yjs";

import {
  checkText,
  encodePlain,
  entryKey,
  has,
  isObject,
  isStructured,
  type JsonObject,
  type Mint,
  OPAQUE_KEYS,
  ORDER_KEY,
  own,
  PROJ_BAD_STATE,
  PROJ_NO_RECORD_URI,
  PROJ_RESERVED_KEY,
  ProjectionError,
  propertyKey,
  put,
  RESERVED_KEYS,
  ROOT_MAP,
  TEXT_CONTENT_TYPES,
  uniqueKey,
  VALUE_KEY,
  valueKey,
} from "./rules.ts";

/** The path between a record URI and the UUID of one of its internal node IRIs. */
export const NODE_PATH = "/node/";

/** Mint a new internal node IRI `<recordUri>/node/<uuidv7>` (R4; `lae.ids.node_iri`). */
export function nodeIri(recordUri: string): string {
  return `${recordUri}${NODE_PATH}${uuidv7()}`;
}

const UUID7 = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** True if `iri` is exactly `<recordUri>/node/<canonical lower-case UUIDv7>` (R15). */
export function isInternalNodeIri(iri: unknown, recordUri: string): boolean {
  const prefix = recordUri + NODE_PATH;
  return typeof iri === "string" && iri.startsWith(prefix) && UUID7.test(iri.slice(prefix.length));
}

/** Return `recordUri`, or the root `id` when it is undefined; else `LAE-PROJ-004`. */
export function resolveRecordUri(recordUri: string | undefined, rootId: unknown): string {
  const uri = recordUri ?? rootId;
  if (typeof uri !== "string" || uri === "") {
    throw new ProjectionError(PROJ_NO_RECORD_URI, "no record URI: pass recordUri or set a root id");
  }
  return uri;
}

/**
 * `assignNodeIris` for one node or reference: a copy in which every embedded node without an
 * `id` has one (R4). Nested nodes are minted before the node that contains them, siblings in key
 * order (R25). The root itself never gets an `id`; nothing inside `@context` is touched.
 */
export function assignObject(obj: JsonObject, mint: Mint, isRoot = false): JsonObject {
  const out: JsonObject = {};
  for (const [key, value] of Object.entries(obj)) {
    if (OPAQUE_KEYS.has(key)) {
      put(out, key, value);
    } else if (isStructured(value)) {
      put(out, key, assignObject(value, mint));
    } else if (Array.isArray(value)) {
      put(
        out,
        key,
        value.map((item: unknown) => (isStructured(item) ? assignObject(item, mint) : item)),
      );
    } else {
      put(out, key, value);
    }
  }
  if (!isRoot && !has(out, "id") && has(out, "type")) {
    put(out, "id", mint());
  }
  return out;
}

/** Return a copy with `id` added to every embedded node that lacks one (R4). */
export function assignNodeIris(jsonld: JsonObject, recordUri: string, mint?: Mint): JsonObject {
  return assignObject(jsonld, mint ?? (() => nodeIri(recordUri)), true);
}

/** True if `value` under `key` of an object of `parentType` is a `Text` (R8.2). */
export function isTextContent(key: string, value: unknown, parentType: unknown): boolean {
  return (
    key === "content" &&
    typeof value === "string" &&
    typeof parentType === "string" &&
    TEXT_CONTENT_TYPES.has(parentType)
  );
}

/**
 * The content of one object map: its property keys and its array entries (R5, R6, R8), as
 * not-yet-integrated Y types and plain values. Every embedded node must already have its `id`.
 */
export function objectFields(obj: JsonObject): Map<string, unknown> {
  const fields = new Map<string, unknown>();
  for (const [key, value] of Object.entries(obj)) {
    checkText(key, "a key");
    if (RESERVED_KEYS.has(key)) {
      throw new ProjectionError(PROJ_RESERVED_KEY, `reserved key ${key} in the input`);
    }
    if (!OPAQUE_KEYS.has(key) && Array.isArray(value) && value.length > 0) {
      for (const [entry, content] of arrayEntries(key, value)) {
        fields.set(entry, content);
      }
    } else {
      fields.set(propertyKey(key), propertyValue(key, value, own(obj, "type")));
    }
  }
  return fields;
}

/** The stored form of a property value that is not a non-empty array (R8.1–3, R8.5). */
export function propertyValue(key: string, value: unknown, parentType: unknown): unknown {
  if (OPAQUE_KEYS.has(key)) {
    return encodePlain(value);
  }
  if (isTextContent(key, value, parentType)) {
    return new Y.Text(checkText(value as string, "content"));
  }
  if (isStructured(value)) {
    return new Y.Map(objectFields(value));
  }
  return encodePlain(value);
}

/** `[base item key, entry content without _order]` for one array item (R6, R7). */
export function entryFields(item: unknown): [string, Map<string, unknown>] {
  if (isStructured(item)) {
    const itemId = own(item, "id");
    if (typeof itemId !== "string") {
      throw new ProjectionError(PROJ_BAD_STATE, "an embedded node has no id: assign IRIs first");
    }
    return [itemId, objectFields(item)];
  }
  return [valueKey(item), new Map([[VALUE_KEY, encodePlain(item)]])];
}

function arrayEntries(prop: string, items: unknown[]): Map<string, Y.Map<unknown>> {
  const used = new Set<string>();
  const entries = new Map<string, Y.Map<unknown>>();
  items.forEach((item, index) => {
    const [base, fields] = entryFields(item);
    fields.set(ORDER_KEY, index);
    const itemKey = uniqueKey(base, used);
    used.add(itemKey);
    entries.set(entryKey(prop, itemKey), new Y.Map(fields));
  });
  return entries;
}

/**
 * Write `jsonld` into the `record` map of an empty `doc` under rules R1–R10 (tests and tools
 * only: the server hydrates, R2). `recordUri` defaults to the root `id`.
 */
export function hydrate(
  jsonld: unknown,
  recordUri?: string,
  options: { mint?: Mint; doc?: Y.Doc } = {},
): Y.Doc {
  if (!isObject(jsonld)) {
    throw new ProjectionError(PROJ_BAD_STATE, "a record must be a JSON object");
  }
  const uri = resolveRecordUri(recordUri, own(jsonld, "id"));
  const fields = objectFields(assignNodeIris(jsonld, uri, options.mint));
  const doc = options.doc ?? new Y.Doc();
  const root = doc.getMap<unknown>(ROOT_MAP);
  doc.transact(() => {
    for (const [key, value] of fields) {
      root.set(key, value);
    }
  });
  return doc;
}
