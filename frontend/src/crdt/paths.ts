/**
 * Operations on a record's `Y.Doc`: the editor's write path.
 *
 * TypeScript mirror of `backend/src/lae/projection/ops.py`; both must leave the same document
 * behind for the same calls (rules R6, R7, R11–R14, R19–R25 of `docs/spikes/S3-projection.md`).
 * The shared cases are `backend/tests/projection/fixtures/*.json`, which `paths.test.ts` runs
 * unchanged. Paths use the UI Profile path syntax of `profiles/README.md`.
 *
 * Conventions for every function that takes a path:
 *
 * - `base` is the object map the path starts from; the default is the root `record` map. One
 *   entry of an array is reached by passing its map (from {@link getEntry}, {@link listEntries}
 *   or {@link getPath}) as `base`, never by encoding its key in the path.
 * - A write follows a single chain of nodes (R20). A missing intermediate node is created (R21);
 *   functions that only read or remove never create anything. A path that is written through
 *   never names `id`, `type` or `_label` (`LAE-PROJ-007`, checked before anything else).
 * - Every write is one Yjs transaction, checked before it changes anything, so a rejected call
 *   leaves the document as it was (R25). `origin` is the transaction origin: the editor passes
 *   its local origin so that its `Y.UndoManager` tracks the change (`useRecordDoc`). A call made
 *   inside the caller's own `doc.transact` joins that transaction and its origin.
 * - `mint` replaces {@link nodeIri} for the internal node IRIs an operation creates; `recordUri`
 *   defaults to the root `id` and is only needed when something is minted.
 * - An entry is named by its **entry key** (the map key, `entryKey(property, item key)`), which
 *   {@link addNode} and {@link addReference} return.
 */
import * as Y from "yjs";

import {
  assignObject,
  entryFields,
  isTextContent,
  nodeIri,
  objectFields,
  resolveRecordUri,
} from "./hydrate.ts";
import { formatSegment, parsePath, type PathSegment } from "./pathSyntax.ts";
import { entryOrder, projectObject, recordMap, sortedEntries } from "./project.ts";
import {
  checkJson,
  checkText,
  codePointLength,
  decodePlain,
  encodePlain,
  entryKey,
  has,
  isEmptyArrayMarker,
  isNode,
  isObject,
  isStructured,
  type JsonObject,
  type Mint,
  OPAQUE_KEYS,
  orderBetween,
  ORDER_KEY,
  own,
  parseKey,
  PROJ_BAD_PATH,
  PROJ_BAD_STATE,
  PROJ_NO_ENTRY,
  PROJ_RESERVED_KEY,
  PROJ_SINGLE_VALUE,
  ProjectionError,
  propertyKey,
  put,
  RESERVED_KEYS,
  SINGLE_VALUED_CLASSES,
  textChange,
  uniqueKey,
  utf16Offset,
  VALUE_KEY,
  valueKey,
  WRITE_FORBIDDEN_KEYS,
} from "./rules.ts";

/** An object map of the document: the root, a node, a reference or an entry. */
export type YMap = Y.Map<unknown>;

/** Options every path operation takes. */
export interface PathOptions {
  /** The object map the path starts from (default: the root `record` map). */
  base?: YMap | null;
}

/** Options of operations that write. */
export interface WriteOptions extends PathOptions {
  /** Transaction origin (the editor's local origin, so that undo tracks the change). */
  origin?: unknown;
}

/** Options of operations that may mint internal node IRIs. */
export interface MintOptions extends WriteOptions {
  /** The record URI for minting; defaults to the root `id`. */
  recordUri?: string;
  /** Replaces {@link nodeIri}; called once per minted IRI, in R25 order. */
  mint?: Mint;
}

/** Options of {@link addNode} and {@link addReference}. */
export interface AddOptions extends MintOptions {
  /** Place the new entry just before this entry key; `null`/absent appends. */
  beforeKey?: string | null;
}

/** Options of {@link addReference}. */
export interface AddReferenceOptions extends AddOptions {
  /** The reference's `type`; defaults to the class filter of the last segment. */
  type?: string | null;
  /** Stored as `_label` (unresolved references only, spec §3.2). */
  label?: string | null;
}

function transact<T>(doc: Y.Doc, origin: unknown, fn: () => T): T {
  return doc.transact(fn, origin ?? null);
}

// ------------------------------------------------------------------------------------------
// Entries of one property of one object map (R12–R14)
// ------------------------------------------------------------------------------------------

/** Renumber the entries of one property to 0, 1, ... in their projected order (R12). */
export function rebalance(parent: YMap, prop: string): void {
  sortedEntries(parent, prop).forEach(([, entry], index) => {
    if (entry.get(ORDER_KEY) !== index) {
      entry.set(ORDER_KEY, index);
    }
  });
}

/**
 * The `_order` that places an entry at `index`, rebalancing first if needed (R12). `skip` is
 * the key of an entry to leave out when finding the neighbours (the entry being moved). Must be
 * called inside the transaction that then writes the result.
 */
export function orderForIndex(
  parent: YMap,
  prop: string,
  index: number,
  skip: string | null = null,
): number {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const orders = sortedEntries(parent, prop)
      .filter(([key]) => key !== skip)
      .map(([, entry]) => entryOrder(entry) as number);
    const at = Math.max(0, Math.min(index, orders.length));
    const before = at > 0 ? (orders[at - 1] as number) : null;
    const after = at < orders.length ? (orders[at] as number) : null;
    const order = orderBetween(before, after);
    if (order !== null) {
      return order;
    }
    if (attempt === 0) {
      rebalance(parent, prop);
    }
  }
  throw new ProjectionError(PROJ_BAD_STATE, "no free _order after rebalance");
}

function sameItem(entry: YMap, item: unknown): boolean {
  if (isStructured(item)) {
    return !entry.has(VALUE_KEY) && entry.get("id") === own(item, "id");
  }
  return entry.has(VALUE_KEY) && valueKey(decodePlain(entry.get(VALUE_KEY))) === valueKey(item);
}

/**
 * The key of the entry of `prop` that already holds `item`, if any (R7, refined): the entry
 * under the item's base item key if that is the same item, else the first entry in projected
 * order that is.
 */
function findItem(existing: Map<string, YMap>, prop: string, item: unknown): string | null {
  let base: string;
  if (!isStructured(item)) {
    base = valueKey(item);
  } else if (has(item, "id")) {
    base = own(item, "id") as string;
  } else {
    return null; // a node that has no id yet is new
  }
  const key = entryKey(prop, base);
  const atBase = existing.get(key);
  if (atBase !== undefined && sameItem(atBase, item)) {
    return key;
  }
  for (const [found, entry] of existing) {
    if (sameItem(entry, item)) {
      return found;
    }
  }
  return null;
}

function defaultMint(doc: Y.Doc, recordUri: string | undefined, mint: Mint | undefined): Mint {
  if (mint !== undefined) {
    return mint;
  }
  return () => nodeIri(resolveRecordUri(recordUri, recordMap(doc).get("id")));
}

/** Throw what storing `item` would throw (R10), without minting anything. */
function validateObject(item: JsonObject): void {
  objectFields(assignObject(item, () => ""));
}

/** Validate `item` and return it with ids assigned, its base item key and its fields. */
function prepareItem(item: unknown, mint: Mint): [unknown, string, Map<string, unknown>] {
  let prepared = item;
  if (isStructured(item)) {
    prepared = assignObject(item, mint);
  } else {
    checkJson(item);
  }
  const [base, fields] = entryFields(prepared);
  return [prepared, base, fields];
}

/** Write one entry (R7, R12, R14); must run inside a transaction. */
function insertPrepared(
  parent: YMap,
  prop: string,
  item: unknown,
  base: string,
  fields: Map<string, unknown>,
  index: number | null,
): string {
  const existing = new Map(sortedEntries(parent, prop));
  const present = findItem(existing, prop, item);
  if (present !== null) {
    return present;
  }
  let key = entryKey(prop, base);
  if (existing.has(key)) {
    // The base item key is held by a suffixed duplicate of another item.
    const used = new Set([...existing.keys()].map((k) => parseKey(k)[1] ?? ""));
    key = entryKey(prop, uniqueKey(base, used));
  }
  const single = propertyKey(prop);
  if (parent.has(single)) {
    // Allowed to go: the marker of an empty source array, and a single value that concurrent
    // edits left beside entries (the projection already ignores it, R11).
    if (!isEmptyArrayMarker(parent.get(single)) && existing.size === 0) {
      throw new ProjectionError(PROJ_SINGLE_VALUE, `${prop} holds a single value`);
    }
    parent.delete(single);
  }
  const position = index ?? existing.size;
  fields.set(ORDER_KEY, orderForIndex(parent, prop, position));
  parent.set(key, new Y.Map(fields));
  return key;
}

/**
 * Insert `item` at position `index` (default: append) of `parent[prop]`; return its entry key.
 * The primitive under {@link addNode} and {@link addReference}, for callers that hold the parent
 * map. Inserting an item the array already holds is a no-op that returns the existing key (R7).
 */
export function insertItem(
  doc: Y.Doc,
  parent: YMap,
  prop: string,
  item: unknown,
  index: number | null = null,
  options: Omit<MintOptions, "base"> = {},
): string {
  if (RESERVED_KEYS.has(prop) || OPAQUE_KEYS.has(prop)) {
    throw new ProjectionError(PROJ_RESERVED_KEY, `${prop} cannot be an array property`);
  }
  const [prepared, base, fields] = prepareItem(
    item,
    defaultMint(doc, options.recordUri, options.mint),
  );
  return transact(doc, options.origin, () =>
    insertPrepared(parent, prop, prepared, base, fields, index),
  );
}

/** Move entry `key` so that it sits at `index` among the other entries of its property. */
export function moveItem(
  doc: Y.Doc,
  parent: YMap,
  key: string,
  index: number,
  options: { origin?: unknown } = {},
): void {
  const [prop] = parseKey(key);
  transact(doc, options.origin, () => {
    const entry = parent.get(key);
    if (!(entry instanceof Y.Map)) {
      throw new ProjectionError(PROJ_NO_ENTRY, `no entry ${key}`);
    }
    const order = orderForIndex(parent, prop, index, key);
    entry.set(ORDER_KEY, order);
  });
}

/** Delete entry `key` of `parent`; nothing else is touched (R12). */
export function removeItem(
  doc: Y.Doc,
  parent: YMap,
  key: string,
  options: { origin?: unknown } = {},
): void {
  transact(doc, options.origin, () => parent.delete(key));
}

// ------------------------------------------------------------------------------------------
// Reading a path (profiles/README.md, "Reading")
// ------------------------------------------------------------------------------------------

/** What `key` holds in `value`: each array item in order, or the single value. */
function step(value: unknown, key: string): unknown[] {
  if (value instanceof Y.Map) {
    const map = value as YMap;
    const entries = sortedEntries(map, key);
    if (entries.length > 0) {
      // Entries win over a single value (R11).
      return entries.map(([, entry]) =>
        entry.has(VALUE_KEY) ? decodePlain(entry.get(VALUE_KEY)) : entry,
      );
    }
    if (!map.has(key)) {
      return [];
    }
    const stored = map.get(key);
    if (stored instanceof Y.Map || stored instanceof Y.Text) {
      return [stored];
    }
    const decoded = decodePlain(stored);
    return Array.isArray(decoded) ? [...(decoded as unknown[])] : [decoded];
  }
  if (isObject(value)) {
    // Inside an opaque value: plain JSON.
    if (!has(value, key)) {
      return [];
    }
    const held = value[key];
    return Array.isArray(held) ? [...(held as unknown[])] : [held];
  }
  return [];
}

/** True if `value` is an object whose `type` is exactly the string `cls`. */
function hasType(value: unknown, cls: string): boolean {
  if (value instanceof Y.Map) {
    return !value.has(VALUE_KEY) && value.get("type") === cls;
  }
  return isObject(value) && own(value, "type") === cls;
}

function select(value: unknown, segment: PathSegment): unknown[] {
  const found = step(value, segment.key);
  const filter = segment.typeFilter;
  return filter === null ? found : found.filter((item) => hasType(item, filter));
}

/**
 * Every value `path` addresses, in document order. Nodes and references come back as their
 * live object maps, `Text` values as `Y.Text`, everything else as decoded JSON;
 * {@link valueToJson} turns any of them into JSON. The result equals evaluating the path on the
 * projection.
 */
export function getPath(doc: Y.Doc, path: string, options: PathOptions = {}): unknown[] {
  let current: unknown[] = [options.base ?? recordMap(doc)];
  for (const segment of parsePath(path)) {
    current = current.flatMap((value) => select(value, segment));
  }
  return current;
}

/** The JSON form of one value returned by {@link getPath}. */
export function valueToJson(value: unknown): unknown {
  if (value instanceof Y.Map) {
    return projectObject(value as YMap);
  }
  if (value instanceof Y.Text) {
    return value.toString();
  }
  return value;
}

// ------------------------------------------------------------------------------------------
// Following a write path (R20, R21)
// ------------------------------------------------------------------------------------------

/**
 * Parse the path of an operation that writes. Naming `id`, `type` or `_label` in any segment
 * is `LAE-PROJ-007`, checked before anything else (R20); then the grammar applies.
 */
function writePath(path: string): PathSegment[] {
  for (const part of path.split(".")) {
    const key = part.replace(/\[.*$/, "");
    if (WRITE_FORBIDDEN_KEYS.has(key)) {
      throw new ProjectionError(PROJ_BAD_PATH, `a write path cannot name ${key}`);
    }
  }
  return parsePath(path);
}

function lastOf(segments: PathSegment[]): PathSegment {
  return segments[segments.length - 1] as PathSegment;
}

/**
 * Follow `segments` (the intermediate steps of a write path) as far as nodes exist. Returns the
 * deepest existing node and the segments missing below it. With `create` the missing segments
 * are checked to be creatable (R21), so that {@link materialize} cannot fail once it writes.
 */
function locate(
  doc: Y.Doc,
  base: YMap | null | undefined,
  segments: PathSegment[],
  create: boolean,
): [YMap, PathSegment[]] {
  let node = base ?? recordMap(doc);
  for (const [position, segment] of segments.entries()) {
    const found = select(node, segment);
    if (found.length > 1) {
      throw new ProjectionError(
        PROJ_BAD_PATH,
        `${formatSegment(segment)} addresses ${String(found.length)} values`,
      );
    }
    if (found.length === 0) {
      const missing = segments.slice(position);
      if (create) {
        checkCreatable(node, missing);
      }
      return [node, missing];
    }
    const next = found[0];
    if (!(next instanceof Y.Map)) {
      throw new ProjectionError(PROJ_BAD_PATH, `${formatSegment(segment)} does not address a node`);
    }
    node = next as YMap;
  }
  return [node, []];
}

function checkCreatable(node: YMap, missing: PathSegment[]): void {
  for (const segment of missing) {
    if (!SINGLE_VALUED_CLASSES.has(segment.key) && segment.typeFilter === null) {
      throw new ProjectionError(
        PROJ_BAD_PATH,
        `cannot create ${formatSegment(segment)}: add a class filter to the path`,
      );
    }
  }
  const first = missing[0] as PathSegment;
  const hasEntries = sortedEntries(node, first.key).length > 0;
  if (SINGLE_VALUED_CLASSES.has(first.key)) {
    // The property holds something the segment does not address; never overwrite it.
    if (node.has(first.key) || hasEntries) {
      throw new ProjectionError(PROJ_BAD_PATH, `${first.key} holds another value`);
    }
  } else if (node.has(first.key) && !isEmptyArrayMarker(node.get(first.key)) && !hasEntries) {
    throw new ProjectionError(PROJ_SINGLE_VALUE, `${first.key} holds a single value`);
  }
}

/** Create the missing intermediate nodes below `node` (R21); must run in a transaction. */
function materialize(node: YMap, missing: PathSegment[], mint: Mint): YMap {
  let current = node;
  for (const segment of missing) {
    const singleClass = SINGLE_VALUED_CLASSES.get(segment.key);
    const created: JsonObject = {};
    put(created, "id", mint());
    put(created, "type", segment.typeFilter ?? singleClass);
    if (singleClass !== undefined) {
      current = current.set(segment.key, new Y.Map(objectFields(created)));
    } else {
      const [, baseKey, fields] = prepareItem(created, mint);
      const key = insertPrepared(current, segment.key, created, baseKey, fields, null);
      current = current.get(key) as YMap;
    }
  }
  return current;
}

/** The node that holds the last segment's property, or `null` if the chain is incomplete. */
function existingParent(
  doc: Y.Doc,
  base: YMap | null | undefined,
  segments: PathSegment[],
): YMap | null {
  const [node, missing] = locate(doc, base, segments.slice(0, -1), false);
  return missing.length > 0 ? null : node;
}

function checkEntryProp(key: string, prop: string): void {
  const [keyProp, itemKey] = parseKey(key);
  if (itemKey === null || keyProp !== prop) {
    throw new ProjectionError(PROJ_NO_ENTRY, `${key} is not an entry key of ${prop}`);
  }
}

/** A copy of `obj` whose `type` is the segment's class filter when it has none (R22). */
function withType(obj: unknown, segment: PathSegment): JsonObject {
  if (!isObject(obj)) {
    throw new ProjectionError(PROJ_BAD_PATH, "the value must be a JSON object");
  }
  const out: JsonObject = {};
  for (const [key, value] of Object.entries(obj)) {
    put(out, key, value);
  }
  if (segment.typeFilter !== null) {
    if (!has(out, "type")) {
      put(out, "type", segment.typeFilter);
    }
    if (own(out, "type") !== segment.typeFilter) {
      throw new ProjectionError(
        PROJ_BAD_PATH,
        `type ${JSON.stringify(own(out, "type"))} does not match ${formatSegment(segment)}`,
      );
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------------
// Entries addressed by a path
// ------------------------------------------------------------------------------------------

/**
 * The array entries the last segment of `path` addresses, as `[entry key, entry map]`, in
 * projected order, restricted to the segment's class filter if it has one. Empty if the chain
 * of nodes before the last segment does not exist.
 */
export function listEntries(doc: Y.Doc, path: string, options: PathOptions = {}): [string, YMap][] {
  const segments = parsePath(path);
  const last = lastOf(segments);
  const parent = existingParent(doc, options.base, segments);
  if (parent === null) {
    return [];
  }
  const entries = sortedEntries(parent, last.key);
  const filter = last.typeFilter;
  return filter === null ? entries : entries.filter(([, entry]) => hasType(entry, filter));
}

/** The entry map with entry key `key` of the array `path` addresses (`LAE-PROJ-008`). */
export function getEntry(doc: Y.Doc, path: string, key: string, options: PathOptions = {}): YMap {
  const segments = parsePath(path);
  checkEntryProp(key, lastOf(segments).key);
  const parent = existingParent(doc, options.base, segments);
  if (parent === null || !parent.has(key)) {
    throw new ProjectionError(PROJ_NO_ENTRY, `no entry ${key}`);
  }
  const entry = parent.get(key);
  if (!(entry instanceof Y.Map)) {
    throw new ProjectionError(PROJ_BAD_STATE, `entry ${key} is not a map`);
  }
  return entry as YMap;
}

function add(
  doc: Y.Doc,
  segments: PathSegment[],
  item: JsonObject,
  options: AddOptions,
  mint: Mint,
): string {
  const prop = lastOf(segments).key;
  const beforeKey = options.beforeKey ?? null;
  if (beforeKey !== null) {
    checkEntryProp(beforeKey, prop);
  }
  validateObject(item);
  const [node, missing] = locate(doc, options.base, segments.slice(0, -1), true);
  let index: number | null = null;
  let existing = new Map<string, YMap>();
  if (missing.length === 0) {
    existing = new Map(sortedEntries(node, prop));
    const present = findItem(existing, prop, item);
    if (present !== null) {
      return present; // already there (R7): nothing is minted or written
    }
    if (node.has(prop) && !isEmptyArrayMarker(node.get(prop)) && existing.size === 0) {
      throw new ProjectionError(PROJ_SINGLE_VALUE, `${prop} holds a single value`);
    }
  }
  if (beforeKey !== null) {
    const keys = missing.length > 0 ? [] : [...existing.keys()];
    if (!keys.includes(beforeKey)) {
      throw new ProjectionError(PROJ_NO_ENTRY, `no entry ${beforeKey}`);
    }
    index = keys.indexOf(beforeKey);
  }
  const [prepared, baseKey, fields] = prepareItem(item, mint);
  return transact(doc, options.origin, () => {
    const parent = materialize(node, missing, mint);
    return insertPrepared(parent, prop, prepared, baseKey, fields, index);
  });
}

/**
 * Add an embedded node to the array `path` addresses and return its entry key. The node, and
 * every node inside it, gets an internal node IRI unless it already has an `id` (R4). It is
 * placed after the last entry, or just before `beforeKey` (R12). A node without `type` takes the
 * class filter of the last segment (R22). A node whose `id` the array already holds is not
 * added again (R7).
 */
export function addNode(
  doc: Y.Doc,
  path: string,
  node: JsonObject,
  options: AddOptions = {},
): string {
  const segments = writePath(path);
  const item = withType(node, lastOf(segments));
  if (!isNode(item)) {
    throw new ProjectionError(PROJ_BAD_PATH, "a node needs a type and, if it has an id, a string");
  }
  return add(doc, segments, item, options, defaultMint(doc, options.recordUri, options.mint));
}

/**
 * Add the reference `{id: uri, type}` to the array `path` addresses and return its entry key.
 * If the array already holds a reference to `uri` nothing changes and that entry's key is
 * returned (R7). `type` defaults to the class filter of the last segment; `label` is stored as
 * `_label`.
 */
export function addReference(
  doc: Y.Doc,
  path: string,
  uri: string,
  options: AddReferenceOptions = {},
): string {
  const segments = writePath(path);
  if (typeof uri !== "string") {
    throw new ProjectionError(PROJ_BAD_PATH, "a reference needs a string id");
  }
  const reference: JsonObject = { id: uri };
  if (options.type !== undefined && options.type !== null) {
    reference.type = options.type;
  }
  if (options.label !== undefined && options.label !== null) {
    reference._label = options.label;
  }
  const item = withType(reference, lastOf(segments));
  return add(doc, segments, item, options, defaultMint(doc, options.recordUri, options.mint));
}

/**
 * Delete the entry with entry key `key` from the array `path` addresses. Returns `false`,
 * changing nothing, if there is no such entry; removing the last entry leaves the property
 * absent (R14).
 */
export function removeEntry(
  doc: Y.Doc,
  path: string,
  key: string,
  options: WriteOptions = {},
): boolean {
  const segments = writePath(path);
  checkEntryProp(key, lastOf(segments).key);
  const parent = existingParent(doc, options.base, segments);
  if (parent === null || !parent.has(key)) {
    return false;
  }
  removeItem(doc, parent, key, options);
  return true;
}

/**
 * Move entry `key` to just before entry `beforeKey`, or to the end if it is `null`. A class
 * filter on the last segment does not restrict the neighbours. Only the moved entry's `_order`
 * is written, unless the gap is exhausted or tied and the property is rebalanced first (R12).
 * Nothing is written if the entry is already in that position.
 */
export function moveEntry(
  doc: Y.Doc,
  path: string,
  key: string,
  beforeKey: string | null,
  options: WriteOptions = {},
): void {
  const segments = writePath(path);
  const prop = lastOf(segments).key;
  checkEntryProp(key, prop);
  if (beforeKey !== null) {
    checkEntryProp(beforeKey, prop);
  }
  const parent = existingParent(doc, options.base, segments);
  const keys = parent === null ? [] : sortedEntries(parent, prop).map(([k]) => k);
  if (parent === null || !keys.includes(key)) {
    throw new ProjectionError(PROJ_NO_ENTRY, `no entry ${key}`);
  }
  if (beforeKey === key) {
    return;
  }
  const others = keys.filter((k) => k !== key);
  let index: number;
  if (beforeKey === null) {
    index = others.length;
  } else if (others.includes(beforeKey)) {
    index = others.indexOf(beforeKey);
  } else {
    throw new ProjectionError(PROJ_NO_ENTRY, `no entry ${beforeKey}`);
  }
  if (keys.indexOf(key) !== index) {
    moveItem(doc, parent, key, index, options);
  }
}

// ------------------------------------------------------------------------------------------
// Single values
// ------------------------------------------------------------------------------------------

/** Replace `deleteCount` code points at code-point position `index` with `insert` (R19). */
function splice(text: Y.Text, index: number, deleteCount: number, insert: string): void {
  const current = text.toString();
  if (index < 0 || deleteCount < 0 || index + deleteCount > codePointLength(current)) {
    throw new ProjectionError(
      PROJ_NO_ENTRY,
      `no text range ${String(index)}+${String(deleteCount)}`,
    );
  }
  const start = utf16Offset(current, index);
  if (deleteCount > 0) {
    text.delete(start, utf16Offset(current, index + deleteCount) - start);
  }
  if (insert !== "") {
    text.insert(start, insert);
  }
}

/**
 * Set the single-valued property `path` addresses to a string, number, boolean or `null`.
 *
 * `content` of a `Name`, `Identifier` or `LinguisticObject` is a `Y.Text` (R8.2): an existing one
 * is edited with the one splice that turns the old string into the new one, so that concurrent
 * edits to other parts of the text survive (R23). Every other value is a last-writer-wins
 * register and is always written. `LAE-PROJ-007` if the property has array entries or holds a
 * node or reference.
 */
export function setScalar(
  doc: Y.Doc,
  path: string,
  value: string | number | boolean | null,
  options: MintOptions = {},
): void {
  const segments = writePath(path);
  const last = lastOf(segments);
  if (last.typeFilter !== null) {
    throw new ProjectionError(PROJ_BAD_PATH, `${formatSegment(last)}: a scalar has no class`);
  }
  if (Array.isArray(value) || isObject(value)) {
    throw new ProjectionError(PROJ_BAD_PATH, "not a scalar");
  }
  const encoded = encodePlain(value);
  const [node, missing] = locate(doc, options.base, segments.slice(0, -1), true);
  if (missing.length === 0) {
    if (sortedEntries(node, last.key).length > 0) {
      throw new ProjectionError(PROJ_BAD_PATH, `${last.key} is an array`);
    }
    if (node.get(last.key) instanceof Y.Map) {
      throw new ProjectionError(PROJ_BAD_PATH, `${last.key} holds a node or reference`);
    }
  }
  transact(doc, options.origin, () => {
    const parent = materialize(node, missing, defaultMint(doc, options.recordUri, options.mint));
    if (isTextContent(last.key, value, parent.get("type"))) {
      const existing = parent.get(last.key);
      if (existing instanceof Y.Text) {
        const [index, deleteCount, insert] = textChange(existing.toString(), value as string);
        splice(existing, index, deleteCount, insert);
      } else {
        parent.set(last.key, new Y.Text(value as string));
      }
    } else {
      parent.set(last.key, encoded);
    }
  });
}

/**
 * Edit the `Y.Text` at `path`: delete `deleteCount` code points at `index`, then insert
 * `insert`. Positions count Unicode code points (R19). `LAE-PROJ-007` if the path does not
 * address a `Text` or an argument has the wrong type; `LAE-PROJ-008` if the range is outside
 * the text.
 */
export function spliceText(
  doc: Y.Doc,
  path: string,
  index: number,
  deleteCount: number,
  insert: string,
  options: WriteOptions = {},
): void {
  const segments = writePath(path);
  const last = lastOf(segments);
  if (typeof insert !== "string") {
    throw new ProjectionError(PROJ_BAD_PATH, "the text to insert must be a string");
  }
  for (const number of [index, deleteCount]) {
    if (typeof number !== "number" || !Number.isInteger(number)) {
      throw new ProjectionError(PROJ_BAD_PATH, "index and delete must be integers");
    }
  }
  checkText(insert, "the text to insert");
  const parent = existingParent(doc, options.base, segments);
  const text = parent === null || last.typeFilter !== null ? undefined : parent.get(last.key);
  if (
    !(text instanceof Y.Text) ||
    (parent !== null && sortedEntries(parent, last.key).length > 0)
  ) {
    throw new ProjectionError(PROJ_BAD_PATH, `${path} does not address a Text`);
  }
  transact(doc, options.origin, () => splice(text, index, deleteCount, insert));
}

/**
 * Set the single-valued property `path` addresses to a node or reference and return its `id`.
 * The whole value is replaced (a last-writer-wins register, R14), which is also how a single
 * reference is pointed at another target (R5). `LAE-PROJ-007` if the property has entries.
 */
export function setObject(
  doc: Y.Doc,
  path: string,
  obj: JsonObject,
  options: MintOptions = {},
): string {
  const segments = writePath(path);
  const last = lastOf(segments);
  const item = withType(obj, last);
  if (!isStructured(item)) {
    throw new ProjectionError(PROJ_BAD_PATH, "not a node or reference");
  }
  validateObject(item);
  const [node, missing] = locate(doc, options.base, segments.slice(0, -1), true);
  if (missing.length === 0 && sortedEntries(node, last.key).length > 0) {
    throw new ProjectionError(PROJ_BAD_PATH, `${last.key} is an array`);
  }
  const mint = defaultMint(doc, options.recordUri, options.mint);
  const assigned = assignObject(item, mint);
  const fields = objectFields(assigned);
  transact(doc, options.origin, () => {
    const parent = materialize(node, missing, mint);
    parent.set(last.key, new Y.Map(fields));
  });
  return own(assigned, "id") as string;
}

/**
 * Delete the single value `path` addresses (a scalar, `Text`, node or reference). Returns
 * `false`, changing nothing, if the property is absent or, with a class filter on the last
 * segment, its value is not an object of that class. `LAE-PROJ-007` if the property has entries
 * (remove those one by one with {@link removeEntry}).
 */
export function removeValue(doc: Y.Doc, path: string, options: WriteOptions = {}): boolean {
  const segments = writePath(path);
  const last = lastOf(segments);
  const parent = existingParent(doc, options.base, segments);
  if (parent === null) {
    return false;
  }
  if (sortedEntries(parent, last.key).length > 0) {
    throw new ProjectionError(PROJ_BAD_PATH, `${last.key} is an array`);
  }
  if (!parent.has(last.key)) {
    return false;
  }
  if (last.typeFilter !== null && !hasType(parent.get(last.key), last.typeFilter)) {
    return false;
  }
  transact(doc, options.origin, () => parent.delete(last.key));
  return true;
}
