/**
 * Test helpers for the CRDT mirror: the shared operation fixtures of the backend, state dumps,
 * and replica sync. The fixture format is defined in
 * `backend/tests/projection/fixture_runner.py`; the files are read unchanged from
 * `backend/tests/projection/fixtures/` (never copied, so they cannot drift).
 */
import * as Y from "yjs";

import {
  addNode,
  addReference,
  getEntry,
  getPath,
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
import { project, recordMap } from "./project.ts";
import { canonicalJson, type JsonObject, type Mint } from "./rules.ts";

/** Raw text of every fixture file, keyed by its path relative to this module. */
export const FIXTURE_FILES = import.meta.glob<string>(
  "../../../backend/tests/projection/fixtures/*.json",
  { eager: true, query: "?raw", import: "default" },
);

export interface FixtureStep {
  sync?: string[];
  client?: string;
  op?: string;
  args?: Record<string, unknown>;
  returns?: unknown;
  error?: string;
}

export interface Fixture {
  description: string;
  rules: string[];
  record_uri: string;
  initial: JsonObject;
  initial_update: string;
  initial_state: unknown;
  clients: Record<string, number>;
  steps: FixtureStep[];
  expected: JsonObject;
  expected_state: unknown;
}

/** `[file stem, parsed fixture]` for every fixture file, sorted by name. */
export function loadFixtures(): [string, Fixture][] {
  return Object.entries(FIXTURE_FILES)
    .map(([path, text]): [string, Fixture] => [
      path.replace(/^.*\/([^/]+)\.json$/, "$1"),
      JSON.parse(text) as Fixture,
    ])
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

/** The state dump of a shared or plain value (format: `fixture_runner.py`). */
export function dumpState(value: unknown): unknown {
  if (value instanceof Y.Map) {
    const map: JsonObject = {};
    for (const key of [...(value as YMap).keys()].sort()) {
      Object.defineProperty(map, key, {
        value: dumpState((value as YMap).get(key)),
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
    return { map };
  }
  if (value instanceof Y.Text) {
    return { text: value.toString() };
  }
  return { plain: value };
}

/** The state dump of a document's `record` map. */
export function docState(doc: Y.Doc): unknown {
  return dumpState(recordMap(doc));
}

/** Exchange updates until every replica has every other replica's changes. */
export function syncDocs(docs: Y.Doc[]): void {
  for (let round = 0; round < 2; round += 1) {
    for (const source of docs) {
      for (const target of docs) {
        if (source !== target) {
          Y.applyUpdate(target, Y.encodeStateAsUpdate(source, Y.encodeStateVector(target)));
        }
      }
    }
  }
}

/** A fresh replica with the given client id that holds `update`. */
export function replica(update: Uint8Array, clientId?: number): Y.Doc {
  const doc = new Y.Doc();
  if (clientId !== undefined) {
    doc.clientID = clientId;
  }
  Y.applyUpdate(doc, update);
  return doc;
}

/** Base64 to bytes (the fixtures' `initial_update`). */
export function fromBase64(text: string): Uint8Array {
  return Uint8Array.from(atob(text), (character) => character.charCodeAt(0));
}

/** A `mint` that hands out the listed IRIs in order and fails when they run out. */
export class ListMint {
  readonly iris: string[];

  constructor(iris: string[]) {
    this.iris = [...iris];
  }

  readonly mint: Mint = () => {
    const iri = this.iris.shift();
    if (iri === undefined) {
      throw new Error("the operation minted more node IRIs than the fixture lists");
    }
    return iri;
  };
}

function resolveBase(doc: Y.Doc, hops: { path: string; key?: string }[]): YMap {
  let base = recordMap(doc);
  for (const hop of hops) {
    if (hop.key !== undefined) {
      base = getEntry(doc, hop.path, hop.key, { base });
    } else {
      const found = getPath(doc, hop.path, { base });
      if (found.length !== 1 || !(found[0] instanceof Y.Map)) {
        throw new Error(`base hop ${hop.path} does not address one node`);
      }
      base = found[0] as YMap;
    }
  }
  return base;
}

/**
 * Call one fixture operation with the fixture's (snake_case) argument names. Returns the
 * operation's result (`null` for operations without one) and the IRIs `mint` did not use.
 */
export function callOperation(
  doc: Y.Doc,
  op: string,
  args: Record<string, unknown>,
): { result: unknown; unused: string[] } {
  const arg = <T>(name: string): T => args[name] as T;
  const base =
    args.base === undefined
      ? null
      : resolveBase(doc, args.base as { path: string; key?: string }[]);
  const minted = new ListMint((args.mint as string[] | undefined) ?? []);
  const mint = minted.mint;
  const path = arg<string>("path");
  let result: unknown = null;
  switch (op) {
    case "project":
      result = project(doc);
      break;
    case "get_path":
      result = getPath(doc, path, { base }).map(valueToJson);
      break;
    case "list_entries":
      result = listEntries(doc, path, { base }).map(([key]) => key);
      break;
    case "set_scalar":
      setScalar(doc, path, arg("value"), { base, mint });
      break;
    case "set_object":
      result = setObject(doc, path, arg("obj"), { base, mint });
      break;
    case "remove_value":
      result = removeValue(doc, path, { base });
      break;
    case "splice_text":
      spliceText(doc, path, arg("index"), arg("delete"), arg("insert"), { base });
      break;
    case "add_node":
      result = addNode(doc, path, arg("node"), { base, mint, beforeKey: arg("before_key") });
      break;
    case "add_reference":
      result = addReference(doc, path, arg("uri"), {
        base,
        mint,
        type: arg("type"),
        label: arg("label"),
        beforeKey: arg("before_key"),
      });
      break;
    case "remove_entry":
      result = removeEntry(doc, path, arg("key"), { base });
      break;
    case "move_entry":
      moveEntry(doc, path, arg("key"), arg<string | null>("before_key") ?? null, { base });
      break;
    default:
      throw new Error(`unknown fixture operation ${op}`);
  }
  return { result, unused: minted.iris };
}

/** Canonical text of a JSON value (sorted keys, code-point order), for byte comparisons. */
export function canonical(value: unknown): string {
  return canonicalJson(value);
}
