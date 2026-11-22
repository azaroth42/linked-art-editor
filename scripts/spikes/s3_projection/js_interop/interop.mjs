// Spike S3 cross-language check: does real Yjs read, project, edit and re-encode what pycrdt
// hydrated?  Not part of the pytest run (it needs `npm install yjs`); see check_interop.py.
//
//   node interop.mjs <fixtures.json> <out.json>
import { readFileSync, writeFileSync } from "node:fs";
import * as Y from "yjs";

const ORDER = "_order";
const VALUE = "_value";
const JSON_KEY = "_json";

// Unicode code-point order (rule R13). JS `<` on strings compares UTF-16 code units instead.
function compareCodePoints(a, b) {
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  for (;;) {
    const ca = ia.next();
    const cb = ib.next();
    if (ca.done || cb.done) return (ca.done ? 0 : 1) - (cb.done ? 0 : 1);
    const d = ca.value.codePointAt(0) - cb.value.codePointAt(0);
    if (d !== 0) return d;
  }
}

// Rule R6: a map key that starts with "[" is the JSON text of [property] or [property, itemKey].
const entryKey = (prop, itemKey) => JSON.stringify([prop, itemKey]);

function parseKey(key) {
  if (!key.startsWith("[")) return [key, null];
  let parts = null;
  try { parts = JSON.parse(key); } catch { /* reported below */ }
  if (Array.isArray(parts) && (parts.length === 1 || parts.length === 2)
      && parts.every((p) => typeof p === "string")) {
    return [parts[0], parts.length === 2 ? parts[1] : null];
  }
  throw new Error("malformed map key " + key);
}

function entryOrder(key, e) {
  const order = e instanceof Y.Map ? e.get(ORDER) : undefined;
  if (typeof order !== "number" || Number.isNaN(order)) throw new Error("no _order on " + key);
  return order;
}

const sortEntries = (entries) =>
  entries.sort((x, y) => entryOrder(...x) - entryOrder(...y) || compareCodePoints(x[0], y[0]));

function sortedEntries(parent, prop) {
  return sortEntries([...parent.entries()].filter(([k]) => {
    const [p, item] = parseKey(k);
    return item !== null && p === prop;
  }));
}

function decodePlain(v) {
  if (v !== null && typeof v === "object") {
    if (Object.keys(v).length === 1 && typeof v[JSON_KEY] === "string") return JSON.parse(v[JSON_KEY]);
    throw new Error("unexpected plain value " + JSON.stringify(v));
  }
  return v;
}

// `out[k] = v` would set the prototype for the key "__proto__" instead of storing it.
const put = (out, k, v) =>
  Object.defineProperty(out, k, { value: v, enumerable: true, writable: true, configurable: true });

function projectObject(m) {
  const out = {};
  const arrays = new Map();
  for (const [k, v] of m.entries()) {
    const [prop, item] = parseKey(k);
    if (item !== null) {
      if (!arrays.has(prop)) arrays.set(prop, []);
      arrays.get(prop).push([k, v]);
    } else if (k !== ORDER && k !== VALUE) {
      put(out, prop, projectValue(v));
    }
  }
  // Entries win over a single value stored for the same property (rule R11).
  for (const [prop, entries] of arrays) {
    put(out, prop, sortEntries(entries).map(([, e]) =>
      e.has(VALUE) ? decodePlain(e.get(VALUE)) : projectObject(e)));
  }
  return out;
}

function projectValue(v) {
  if (v instanceof Y.Text) return v.toString();
  if (v instanceof Y.Map) return projectObject(v);
  return decodePlain(v);
}

function orderBetween(before, after) {
  if (before === null && after === null) return 0.0;
  let c, ok;
  if (before === null) { c = after - 1.0; ok = c < after; }
  else if (after === null) { c = before + 1.0; ok = c > before; }
  else { c = (before + after) / 2.0; ok = before < c && c < after; }
  return ok && Number.isFinite(c) ? c : null;
}

function nameEntry(id, content, order) {
  const entry = new Y.Map();
  entry.set("id", id);
  entry.set("type", "Name");
  entry.set("content", new Y.Text(content));
  entry.set("value", 34); // a JS number: integral, so it must project as an integer
  entry.set(ORDER, order);
  return entry;
}

const b64 = (s) => new Uint8Array(Buffer.from(s, "base64"));
const toB64 = (u) => Buffer.from(u).toString("base64");
const fixtures = JSON.parse(readFileSync(process.argv[2], "utf8"));
const results = [];
for (const f of fixtures.documents) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, b64(f.update));
  const record = doc.getMap("record");
  const projected = projectObject(record);
  // Yjs re-encodes every value from its own in-memory form.
  const reencoded = toB64(Y.encodeStateAsUpdate(doc));

  // Insert a Name at position 1 of identified_by with the shared midpoint arithmetic.
  let insert = null;
  const names = sortedEntries(record, "identified_by");
  if (names.length >= 2) {
    const before = Y.encodeStateVector(doc);
    const order = orderBetween(entryOrder(...names[0]), entryOrder(...names[1]));
    const id = f.record_uri + "/node/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
    record.set(entryKey("identified_by", id), nameEntry(id, "from JS \u{1F600}", order));
    insert = { order, update: toB64(Y.encodeStateAsUpdate(doc, before)) };
  }

  // First insert into a property that does not exist, concurrently with a Python client that
  // does the same (its update is in the fixture). No container is created (rule R14).
  const before = Y.encodeStateVector(doc);
  if (sortedEntries(record, fixtures.first_prop).length !== 0) throw new Error("not absent");
  const id = f.record_uri + "/node/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e60";
  record.set(entryKey(fixtures.first_prop, id), nameEntry(id, "first from JS", orderBetween(null, null)));
  const first = { update: toB64(Y.encodeStateAsUpdate(doc, before)) };
  Y.applyUpdate(doc, b64(f.python_first));
  const merged = projectObject(record);

  results.push({ name: f.name, projected, reencoded, insert, first, merged });
}

const tie = ["\u{1F600}", "～", "a", "b"].sort(compareCodePoints);
const keys = fixtures.key_parts.map(([prop, item]) => entryKey(prop, item));
writeFileSync(process.argv[3], JSON.stringify({ yjs: "13", results, tie, keys }));
console.log(`processed ${results.length} documents`);
