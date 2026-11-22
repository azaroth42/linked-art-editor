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

const entryOrder = (e) =>
  e instanceof Y.Map && typeof e.get(ORDER) === "number" && !Number.isNaN(e.get(ORDER))
    ? e.get(ORDER)
    : null;

function isContainer(m) {
  for (const v of m.values()) if (entryOrder(v) === null) return false;
  return true;
}

function sortedEntries(container) {
  return [...container.entries()].sort(
    (x, y) => entryOrder(x[1]) - entryOrder(y[1]) || compareCodePoints(x[0], y[0]),
  );
}

function decodePlain(v) {
  if (v !== null && typeof v === "object") {
    if (Object.keys(v).length === 1 && typeof v[JSON_KEY] === "string") return JSON.parse(v[JSON_KEY]);
    throw new Error("unexpected plain value " + JSON.stringify(v));
  }
  return v;
}

function projectObject(m) {
  const out = {};
  for (const [k, v] of m.entries()) {
    if (k === ORDER) continue;
    const p = projectValue(v);
    if (p !== undefined) out[k] = p;
  }
  return out;
}

function projectValue(v) {
  if (v instanceof Y.Text) return v.toString();
  if (v instanceof Y.Map) {
    if (!isContainer(v)) return projectObject(v);
    if (v.size === 0) return undefined;
    return sortedEntries(v).map(([, e]) => (e.has(VALUE) ? decodePlain(e.get(VALUE)) : projectObject(e)));
  }
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

const b64 = (s) => new Uint8Array(Buffer.from(s, "base64"));
const fixtures = JSON.parse(readFileSync(process.argv[2], "utf8"));
const results = [];
for (const f of fixtures) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, b64(f.update));
  const record = doc.getMap("record");
  const projected = projectObject(record);
  // Yjs re-encodes every value from its own in-memory form.
  const reencoded = Buffer.from(Y.encodeStateAsUpdate(doc)).toString("base64");

  // Insert a Name at position 1 of identified_by with the shared midpoint arithmetic.
  let insert = null;
  const names = record.get("identified_by");
  if (names instanceof Y.Map && names.size >= 2) {
    const before = Y.encodeStateVector(doc);
    const sorted = sortedEntries(names);
    const order = orderBetween(entryOrder(sorted[0][1]), entryOrder(sorted[1][1]));
    const key = f.record_uri + "/node/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
    const entry = new Y.Map();
    entry.set("id", key);
    entry.set("type", "Name");
    entry.set("content", new Y.Text("from JS \u{1F600}"));
    entry.set("value", 34); // a JS number: integral, so it must project as an integer
    entry.set(ORDER, order);
    names.set(key, entry);
    insert = { order, update: Buffer.from(Y.encodeStateAsUpdate(doc, before)).toString("base64") };
  }
  results.push({ name: f.name, projected, reencoded, insert });
}

const tie = ["\u{1F600}", "～", "a", "b"].sort(compareCodePoints);
writeFileSync(process.argv[3], JSON.stringify({ yjs: "13", results, tie }));
console.log(`processed ${results.length} documents`);
