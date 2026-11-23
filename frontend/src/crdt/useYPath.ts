/**
 * `useYPath(doc, path, {base})`: the values a profile path addresses, re-rendering when they
 * change (Step 1.17). An object map's change event covers its own properties and its array
 * entries (R6), so the deep observer filters events by the **parsed property names** along the
 * event's path and only re-reads the path when one could have changed what it addresses.
 */
import { useMemo, useSyncExternalStore } from "react";
import * as Y from "yjs";

import { parsePath, type PathSegment } from "./pathSyntax.ts";
import { getPath, valueToJson, type YMap } from "./paths.ts";
import { recordMap } from "./project.ts";
import { canonicalJson, ORDER_KEY, parseKey, VALUE_KEY } from "./rules.ts";

export interface YPathValue {
  /** Live values as `getPath` returns them (object maps, `Y.Text`, plain JSON). */
  values: unknown[];
  /** The JSON of each value. */
  json: unknown[];
  /** Why the path could not be read (a malformed path or an unprojectable document). */
  error: Error | null;
}

const EMPTY: YPathValue = { values: [], json: [], error: null };
/** Keys inside an addressed value's map that change which values a path yields, or their order. */
const SHAPE_KEYS = new Set([ORDER_KEY, VALUE_KEY, "type"]);

function propertyName(hop: string | number): string | null {
  if (typeof hop !== "string") {
    return null;
  }
  try {
    return parseKey(hop)[0];
  } catch {
    return null;
  }
}

/** Whether a change at `names` (property names from the observed map down) can affect `segments`. */
export function affects(names: (string | null)[], segments: PathSegment[]): boolean {
  for (const [index, name] of names.entries()) {
    if (index >= segments.length) {
      return true; // inside an addressed value
    }
    if (name === null) {
      return true; // not a map key: be safe
    }
    if (index > 0 && SHAPE_KEYS.has(name)) {
      return true; // order, opaque item or class of the values one level up
    }
    if (name !== (segments[index] as PathSegment).key) {
      return false;
    }
  }
  return true;
}

function eventAffects(event: Y.YEvent<Y.AbstractType<unknown>>, segments: PathSegment[]) {
  const names = event.path.map(propertyName);
  if (event.target instanceof Y.Map) {
    return [...event.keys.keys()].some((key) => affects([...names, propertyName(key)], segments));
  }
  return affects(names, segments);
}

function read(doc: Y.Doc, path: string, base: YMap | null): YPathValue {
  try {
    const values = getPath(doc, path, { base });
    return { values, json: values.map(valueToJson), error: null };
  } catch (error) {
    return {
      values: [],
      json: [],
      error: error instanceof Error ? error : new Error(String(error)),
    };
  }
}

function same(a: YPathValue, b: YPathValue): boolean {
  return (
    a.error?.message === b.error?.message &&
    a.values.length === b.values.length &&
    a.values.every((value, index) =>
      value instanceof Y.AbstractType ? value === b.values[index] : true,
    ) &&
    canonicalJson(a.json) === canonicalJson(b.json)
  );
}

function createStore(doc: Y.Doc | null, path: string, base: YMap | null) {
  let segments: PathSegment[] = [];
  let snapshot: YPathValue = EMPTY;
  if (doc !== null) {
    try {
      segments = parsePath(path);
    } catch {
      segments = [];
    }
    snapshot = read(doc, path, base);
  }
  return {
    getSnapshot: () => snapshot,
    subscribe(onChange: () => void): () => void {
      if (doc === null) {
        return () => undefined;
      }
      const target = base ?? recordMap(doc);
      const observer = (events: Y.YEvent<Y.AbstractType<unknown>>[]) => {
        if (!events.some((event) => eventAffects(event, segments))) {
          return;
        }
        const next = read(doc, path, base);
        if (!same(next, snapshot)) {
          snapshot = next;
          onChange();
        }
      };
      target.observeDeep(observer);
      // Catch up on anything that changed between the first read and the subscription.
      const fresh = read(doc, path, base);
      if (!same(fresh, snapshot)) {
        snapshot = fresh;
        onChange();
      }
      return () => target.unobserveDeep(observer);
    },
  };
}

/** The values `path` addresses in `doc` (from `base`), kept current. */
export function useYPath(
  doc: Y.Doc | null,
  path: string,
  options: { base?: YMap | null } = {},
): YPathValue {
  const base = options.base ?? null;
  const store = useMemo(() => createStore(doc, path, base), [doc, path, base]);
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}
