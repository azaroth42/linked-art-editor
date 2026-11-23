/**
 * Compound edits the widgets make, each one Yjs transaction with the editor's local origin (so
 * one undo step), built from the `paths.ts` operations.
 */
import * as Y from "yjs";

import {
  addReference,
  getPath,
  listEntries,
  removeEntry,
  removeValue,
  setObject,
  valueToJson,
  type YMap,
} from "../crdt/paths.ts";
import { readReference, type Reference } from "./vocab.ts";

/** A reference to write: `_label` only when given (references picked in this session). */
export interface NewReference {
  id: string;
  type: string;
  label?: string | null;
}

/** The references at `path` from `base`, with their entry keys (`null` for a single value). */
export function referencesAt(
  doc: Y.Doc,
  path: string,
  base: YMap | null,
): { key: string | null; reference: Reference }[] {
  const entries = listEntries(doc, path, { base });
  if (entries.length > 0) {
    return entries.flatMap(([key, map]) => {
      const reference = readReference(valueToJson(map));
      return reference === null ? [] : [{ key, reference }];
    });
  }
  const single = getPath(doc, path, { base })[0];
  const reference = single instanceof Y.Map ? readReference(valueToJson(single)) : null;
  return reference === null ? [] : [{ key: null, reference }];
}

/**
 * Remove the references at `path` that `remove` selects, then add `add` (if given) at the end
 * of the array, all in one transaction.
 */
export function replaceReferences(
  doc: Y.Doc,
  path: string,
  options: {
    base: YMap | null;
    origin: object;
    remove: (reference: Reference) => boolean;
    add?: NewReference | null;
  },
): void {
  const { base, origin, remove, add } = options;
  doc.transact(() => {
    for (const { key, reference } of referencesAt(doc, path, base)) {
      if (!remove(reference)) {
        continue;
      }
      if (key === null) {
        removeValue(doc, path, { base, origin });
      } else {
        removeEntry(doc, path, key, { base, origin });
      }
    }
    if (add !== undefined && add !== null) {
      addReference(doc, path, add.id, { base, origin, type: add.type, label: add.label ?? null });
    }
  }, origin);
}

/** Remove one reference found by {@link referencesAt}: an array entry, or the single value. */
export function removeReference(
  doc: Y.Doc,
  path: string,
  key: string | null,
  options: { base: YMap | null; origin: object },
): void {
  if (key === null) {
    removeValue(doc, path, options);
  } else {
    removeEntry(doc, path, key, options);
  }
}

/** Set the single-valued reference at `path` (e.g. a dimension's `unit`), or remove it. */
export function setReference(
  doc: Y.Doc,
  path: string,
  reference: NewReference | null,
  options: { base: YMap | null; origin: object },
): void {
  const { base, origin } = options;
  if (reference === null) {
    removeValue(doc, path, { base, origin });
    return;
  }
  const object: Record<string, unknown> = { id: reference.id, type: reference.type };
  if (reference.label !== undefined && reference.label !== null) {
    object._label = reference.label;
  }
  setObject(doc, path, object, { base, origin });
}
