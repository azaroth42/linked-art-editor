/**
 * The projection rules that need no Y document: constants, classification, encodings, keys.
 *
 * TypeScript mirror of `backend/src/lae/projection/rules.py`. The specification is rules R1–R25
 * of `docs/spikes/S3-projection.md` (ADR-0018); comments cite them as `R<n>`. Both sides must
 * compute the same results from the same inputs, except where JavaScript cannot tell an integer
 * from a float with the same value (R7, R9: a number typed in the browser is always a double).
 *
 * Objects built here from JSON input are copied with {@link put} and read with {@link own}, never
 * with `obj[key] = value` or `key in obj`: a key such as `__proto__` or `constructor` is an
 * ordinary JSON key and must not reach the prototype chain.
 */
import * as Y from "yjs";

export const ROOT_MAP = "record";
/** Key of the fractional index carried by every array entry (R6, R12). */
export const ORDER_KEY = "_order";
/** Key that holds the item of an entry whose item is opaque (R6). */
export const VALUE_KEY = "_value";
/** The single key of the plain wrapper that stores exact JSON text (R9). */
export const JSON_KEY = "_json";
/** Keys that a node, a reference or the root may not have in the input (R5). */
export const RESERVED_KEYS: ReadonlySet<string> = new Set([ORDER_KEY, VALUE_KEY]);
/** Types whose string `content` is a shared `Text` (R8.2). */
export const TEXT_CONTENT_TYPES: ReadonlySet<string> = new Set([
  "Name",
  "Identifier",
  "LinguisticObject",
]);
/** The only keys a reference may have (R3). */
export const REFERENCE_KEYS: ReadonlySet<string> = new Set(["id", "type", "_label"]);
/** Keys whose value is opaque at any depth, whatever its form (R3). */
export const OPAQUE_KEYS: ReadonlySet<string> = new Set(["@context"]);
/**
 * Keys a write path may not name (R5, R20): `id` and `type` are the identity of an object, and
 * `_label` is never edited through a path (R17; the path grammar of `profiles/README.md` has no
 * `_`-prefixed keys at all).
 */
export const WRITE_FORBIDDEN_KEYS: ReadonlySet<string> = new Set(["id", "type", "_label"]);
/** A map key that starts with this is the key text of an entry key or an escaped property key. */
export const ENTRY_KEY_PREFIX = "[";
/** Integers up to this magnitude are exact as IEEE doubles (R9). */
export const MAX_SAFE_INT = 2 ** 53;

/**
 * Single-valued properties whose value is an embedded node, with the class of that node: a write
 * path creates a missing intermediate for one of these as a single value (R21).
 */
export const SINGLE_VALUED_CLASSES: ReadonlyMap<string, string> = new Map([
  ["timespan", "TimeSpan"],
  ["produced_by", "Production"],
  ["created_by", "Creation"],
  ["destroyed_by", "Destruction"],
  ["born", "Birth"],
  ["died", "Death"],
  ["formed_by", "Formation"],
  ["dissolved_by", "Dissolution"],
  ["duration", "Dimension"],
  ["paid_amount", "MonetaryAmount"],
]);

export const PROJ_RESERVED_KEY = "LAE-PROJ-001";
export const PROJ_BAD_NUMBER = "LAE-PROJ-002";
export const PROJ_BAD_UNICODE = "LAE-PROJ-003";
export const PROJ_NO_RECORD_URI = "LAE-PROJ-004";
export const PROJ_BAD_STATE = "LAE-PROJ-005";
export const PROJ_SINGLE_VALUE = "LAE-PROJ-006";
export const PROJ_BAD_PATH = "LAE-PROJ-007";
export const PROJ_NO_ENTRY = "LAE-PROJ-008";

/**
 * A document cannot be projected, or an operation cannot be applied. `code` is the diagnostics
 * code (the `errors` i18n key); `detail` is for logs and tests only, never shown to users.
 */
export class ProjectionError extends Error {
  readonly code: string;
  readonly detail: string;

  constructor(code: string, detail: string) {
    super(`${code}: ${detail}`);
    this.name = "ProjectionError";
    this.code = code;
    this.detail = detail;
  }
}

/** A JSON object (not an array, not `null`, not a Y type). */
export type JsonObject = Record<string, unknown>;

/** Returns a fresh internal node IRI on every call. */
export type Mint = () => string;

// ------------------------------------------------------------------------------------------
// Safe object access
// ------------------------------------------------------------------------------------------

/** True for a plain JSON object. */
export function isObject(value: unknown): value is JsonObject {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    !(value instanceof Y.AbstractType) &&
    !(value instanceof Uint8Array)
  );
}

/** `obj[key]` if `obj` has `key` as an own property, else `undefined` (never the prototype). */
export function own(obj: JsonObject, key: string): unknown {
  return Object.hasOwn(obj, key) ? obj[key] : undefined;
}

/** `Object.hasOwn`, the JSON meaning of "the object has this key". */
export function has(obj: JsonObject, key: string): boolean {
  return Object.hasOwn(obj, key);
}

/** Store `value` under `key` as an own property; `__proto__` is stored, not followed. */
export function put(obj: JsonObject, key: string, value: unknown): void {
  Object.defineProperty(obj, key, { value, enumerable: true, writable: true, configurable: true });
}

// ------------------------------------------------------------------------------------------
// Classification (R3)
// ------------------------------------------------------------------------------------------

/** True for a reference: a string `id` and no keys beyond `id`, `type`, `_label`. */
export function isReference(obj: unknown): obj is JsonObject {
  return (
    isObject(obj) &&
    typeof own(obj, "id") === "string" &&
    Object.keys(obj).every((key) => REFERENCE_KEYS.has(key))
  );
}

/** True for an object that has `type` and whose `id` is absent or a string. */
export function isNode(obj: unknown): obj is JsonObject {
  return (
    isObject(obj) && has(obj, "type") && (!has(obj, "id") || typeof own(obj, "id") === "string")
  );
}

/** True if `obj` is stored as a shared `Map` (a node or a reference); else it is opaque. */
export function isStructured(obj: unknown): obj is JsonObject {
  return isReference(obj) || isNode(obj);
}

// ------------------------------------------------------------------------------------------
// Plain values (R9, R10)
// ------------------------------------------------------------------------------------------

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

/** Return `value`; throw `LAE-PROJ-003` if it holds a lone surrogate (R10). */
export function checkText(value: string, where: string): string {
  if (LONE_SURROGATE.test(value)) {
    throw new ProjectionError(PROJ_BAD_UNICODE, `${where} is not valid Unicode`);
  }
  return value;
}

/** Throw unless `value` is JSON that can be stored: R10 numbers, strings and keys. */
export function checkJson(value: unknown): void {
  if (value === null || typeof value === "boolean") {
    return;
  }
  if (typeof value === "string") {
    checkText(value, "a string");
  } else if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ProjectionError(PROJ_BAD_NUMBER, `number ${String(value)} is not finite`);
    }
  } else if (Array.isArray(value)) {
    for (const item of value) {
      checkJson(item);
    }
  } else if (isObject(value)) {
    for (const [key, item] of Object.entries(value)) {
      checkText(key, "a key");
      checkJson(item);
    }
  } else {
    throw new ProjectionError(PROJ_BAD_STATE, `not a JSON value: ${typeof value}`);
  }
}

/** Unicode code-point order (R13); `<` on strings compares UTF-16 code units instead. */
export function compareCodePoints(a: string, b: string): number {
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  for (;;) {
    const ca = ia.next();
    const cb = ib.next();
    if (ca.done === true || cb.done === true) {
      return (ca.done === true ? 0 : 1) - (cb.done === true ? 0 : 1);
    }
    const difference = (ca.value.codePointAt(0) ?? 0) - (cb.value.codePointAt(0) ?? 0);
    if (difference !== 0) {
      return difference;
    }
  }
}

function stringify(value: unknown, indent: string, depth: number): string {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    const items = value.map((item) => stringify(item, indent, depth + 1));
    return wrap("[", items, "]", indent, depth);
  }
  if (isObject(value)) {
    const keys = Object.keys(value).sort(compareCodePoints);
    if (keys.length === 0) {
      return "{}";
    }
    const separator = indent === "" ? ":" : ": ";
    const members = keys.map(
      (key) => JSON.stringify(key) + separator + stringify(value[key], indent, depth + 1),
    );
    return wrap("{", members, "}", indent, depth);
  }
  const text = JSON.stringify(value);
  if (text === undefined) {
    throw new ProjectionError(PROJ_BAD_STATE, `not a JSON value: ${typeof value}`);
  }
  return text;
}

function wrap(open: string, items: string[], close: string, indent: string, depth: number) {
  if (indent === "") {
    return open + items.join(",") + close;
  }
  const inner = "\n" + indent.repeat(depth + 1);
  return open + inner + items.join("," + inner) + "\n" + indent.repeat(depth) + close;
}

/**
 * JSON text with object keys sorted in code-point order (R16), compact unless `indent` is given.
 * The same ordering as Python's `json.dumps(sort_keys=True)` and `canonical_json`; numbers use
 * JavaScript's formatting (`33` where Python writes `33.0`, R9).
 */
export function canonicalJson(value: unknown, indent = 0): string {
  return stringify(value, " ".repeat(indent), 0);
}

/** Exact JSON text of `value`: sorted keys, compact, non-ASCII verbatim, no NaN (R9). */
export function jsonText(value: unknown): string {
  checkJson(value);
  return canonicalJson(value);
}

/**
 * Encode a JSON value that is not a node, reference or property-level array (R9).
 *
 * A finite number is stored as a Y number (always a double, so it projects as an integer
 * exactly when it is integral); an object or array becomes a `_json` wrapper.
 */
export function encodePlain(value: unknown): unknown {
  if (value === null || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    return checkText(value, "a string");
  }
  if (typeof value === "number") {
    checkJson(value);
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value) || isObject(value)) {
    return { [JSON_KEY]: jsonText(value) };
  }
  throw new ProjectionError(PROJ_BAD_STATE, `not a JSON value: ${typeof value}`);
}

/** Decode a plain value read from a `Map` back to JSON (inverse of {@link encodePlain}). */
export function decodePlain(value: unknown): unknown {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ProjectionError(PROJ_BAD_STATE, `non-finite number ${String(value)}`);
    }
    return value;
  }
  if (isObject(value)) {
    const keys = Object.keys(value);
    const text = own(value, JSON_KEY);
    if (keys.length === 1 && keys[0] === JSON_KEY && typeof text === "string") {
      try {
        return JSON.parse(text) as unknown;
      } catch (error) {
        throw new ProjectionError(PROJ_BAD_STATE, `malformed _json text: ${String(error)}`);
      }
    }
  }
  throw new ProjectionError(PROJ_BAD_STATE, "unexpected plain value in the document");
}

/** True for `plain([])`, the property-key marker of an array that was empty in the source. */
export function isEmptyArrayMarker(value: unknown): boolean {
  return (
    isObject(value) && Object.keys(value).length === 1 && own(value, JSON_KEY) === "[]" // R8.5
  );
}

// ------------------------------------------------------------------------------------------
// Keys (R6, R7)
// ------------------------------------------------------------------------------------------

/** JSON text of a list of strings; identical to Python's `rules.key_text`. */
export function keyText(parts: string[]): string {
  return JSON.stringify(parts);
}

/** Map key of a property that has no entries: the JSON key, escaped if it starts with `[`. */
export function propertyKey(prop: string): string {
  return prop.startsWith(ENTRY_KEY_PREFIX) ? keyText([prop]) : prop;
}

/** Map key of one array item of property `prop`: the key text of `[prop, itemKey]`. */
export function entryKey(prop: string, itemKey: string): string {
  return keyText([prop, itemKey]);
}

/** `[property, item key]` of a map key; the item key is `null` for a property key. */
export function parseKey(key: string): [string, string | null] {
  if (!key.startsWith(ENTRY_KEY_PREFIX)) {
    return [key, null];
  }
  let parts: unknown = null;
  try {
    parts = JSON.parse(key);
  } catch {
    parts = null;
  }
  if (
    Array.isArray(parts) &&
    (parts.length === 1 || parts.length === 2) &&
    parts.every((part) => typeof part === "string")
  ) {
    const [prop, item] = parts as [string, string | undefined];
    return [prop, item ?? null];
  }
  throw new ProjectionError(PROJ_BAD_STATE, `malformed map key ${JSON.stringify(key)}`);
}

/** Base item key of an opaque array item (R7). */
export function valueKey(value: unknown): string {
  if (typeof value === "string") {
    return "=s:" + value;
  }
  if (value === null) {
    return "=z";
  }
  if (typeof value === "boolean") {
    return value ? "=b:true" : "=b:false";
  }
  if (typeof value === "number") {
    return "=n:" + jsonText(value);
  }
  return "=j:" + jsonText(value);
}

/** `base` if unused, else the first unused of `base #2`, `base #3`, ... (R7). */
export function uniqueKey(base: string, used: ReadonlySet<string>): string {
  let candidate = base;
  for (let n = 2; used.has(candidate); n += 1) {
    candidate = `${base} #${String(n)}`;
  }
  return candidate;
}

// ------------------------------------------------------------------------------------------
// _order arithmetic (R12)
// ------------------------------------------------------------------------------------------

/** The `_order` for a slot between two neighbours, or `null` if a rebalance is needed. */
export function orderBetween(before: number | null, after: number | null): number | null {
  if (before === null && after === null) {
    return 0.0;
  }
  let candidate: number;
  let ok: boolean;
  if (before === null) {
    candidate = (after as number) - 1.0;
    ok = candidate < (after as number);
  } else if (after === null) {
    candidate = before + 1.0;
    ok = candidate > before;
  } else {
    candidate = (before + after) / 2.0;
    ok = before < candidate && candidate < after;
  }
  return ok && Number.isFinite(candidate) ? candidate : null;
}

// ------------------------------------------------------------------------------------------
// Text positions (R19, R23)
// ------------------------------------------------------------------------------------------

/** The Yjs (UTF-16 code unit) offset of code-point position `index` in `text` (R19). */
export function utf16Offset(text: string, index: number): number {
  let offset = 0;
  let count = 0;
  for (const character of text) {
    if (count === index) {
      break;
    }
    offset += character.length;
    count += 1;
  }
  return offset;
}

/** Length of `text` in Unicode code points. */
export function codePointLength(text: string): number {
  return Array.from(text).length;
}

/**
 * The single splice that turns `old` into `new`: `[index, delete count, insert]`, counted in
 * code points (R23): `index` is the length of the longest common prefix, and the longest common
 * suffix of what remains on both sides is kept. No normalization.
 */
export function textChange(old: string, next: string): [number, number, string] {
  const a = Array.from(old);
  const b = Array.from(next);
  const limit = Math.min(a.length, b.length);
  let prefix = 0;
  while (prefix < limit && a[prefix] === b[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (suffix < limit - prefix && a[a.length - 1 - suffix] === b[b.length - 1 - suffix]) {
    suffix += 1;
  }
  return [prefix, a.length - prefix - suffix, b.slice(prefix, b.length - suffix).join("")];
}
