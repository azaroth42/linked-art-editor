/**
 * A record's root `_label`, derived from its primary name (Step 1.19a, ADR-0036): the TypeScript
 * mirror of `backend/src/lae/labels/derive.py` and of the name selection of
 * `lae.labels.extract`. Both must give the same label and the same derived document for every
 * JSON object; `backend/tests/labels/data/derived_label_cases.json` holds the shared cases.
 *
 * The label is never written into the `Y.Doc`. It is a pure function of the record's names and
 * the installation's label language, applied wherever the browser reads the live draft as
 * JSON-LD ({@link projectDraft}: the raw inspector, the validation drawer, the publish
 * dialog's snapshot), as the server applies it in `Room.snapshot`. So it converges with the
 * document, creates no undo entry and fires no update.
 *
 * - A **usable name** is an item of the root `identified_by` (a single object counts as a
 *   one-item array) that is an object with `type` `"Name"` and a string `content` holding a
 *   non-whitespace character (Python's whitespace, {@link WHITESPACE_CLASS}).
 * - **Selection**: the first usable name classified as Primary Name (`aat:300404670` in any AAT
 *   spelling) whose language ({@link primaryLanguageOf} of its first `language` entry) is the
 *   label language; else the first one classified as Primary Name; else the first usable name.
 * - The label is that name's `content` normalized ({@link normalizeLabel}); without a usable
 *   name the document is left as it is (its own `_label` kept, an absent one absent).
 *
 * The label language is always an argument: the record's `labelLanguage`, which
 * `GET /api/records/{id}` sends as `X-Label-Language` (`fetchEditorRecord`). This module holds
 * no language of its own.
 */
import type * as Y from "yjs";

import { aatId, primaryLanguageOf, stripWhitespace, WHITESPACE_CLASS } from "./langtags.ts";
import { project } from "./project.ts";
import { isObject, type JsonObject, own } from "./rules.ts";

/** The root key the derived label is stored under. */
export const LABEL_KEY = "_label";

/** AAT *preferred terms* (Linked Art's Primary Name), as the digits after `aat/`. */
export const PRIMARY_NAME_AAT_ID = "300404670";

const WHITESPACE_RUN = new RegExp(`[${WHITESPACE_CLASS}]+`, "gu");

/**
 * `text` stripped, with every run of whitespace replaced by one U+0020: Python's
 * `" ".join(text.split())`. Nothing else changes: no Unicode normalization, no case folding,
 * no truncation.
 */
export function normalizeLabel(text: string): string {
  return stripWhitespace(text).replace(WHITESPACE_RUN, " ");
}

/** JSON-LD's single object where an array is usual, as a one-item array; `null` as none. */
function asList(value: unknown): unknown[] {
  if (Array.isArray(value)) {
    return value;
  }
  return value === undefined || value === null ? [] : [value];
}

/** True if `name` is classified as Primary Name (Python's `is_primary_name`). */
export function isPrimaryName(name: JsonObject): boolean {
  return asList(own(name, "classified_as")).some((entry) => {
    if (!isObject(entry)) {
      return false;
    }
    const id = own(entry, "id");
    return typeof id === "string" && aatId(id) === PRIMARY_NAME_AAT_ID;
  });
}

/** One usable name: its content, its primary language and whether it is a Primary Name. */
interface UsableName {
  content: string;
  language: string;
  primary: boolean;
}

/** The usable names of the record, in array order (Python's `iter_names`). */
function usableNames(jsonld: JsonObject): UsableName[] {
  const names: UsableName[] = [];
  for (const name of asList(own(jsonld, "identified_by"))) {
    if (!isObject(name) || own(name, "type") !== "Name") {
      continue;
    }
    const content = own(name, "content");
    if (typeof content !== "string" || stripWhitespace(content) === "") {
      continue;
    }
    const languages = asList(own(name, "language"));
    names.push({
      content,
      language: languages.length > 0 ? primaryLanguageOf(languages[0]) : "und",
      primary: isPrimaryName(name),
    });
  }
  return names;
}

/** The selected name (Python's `primary_name`), or `undefined` without a usable name. */
function primaryName(names: UsableName[], labelLanguage: string): UsableName | undefined {
  const primaries = names.filter((name) => name.primary);
  return primaries.find((name) => name.language === labelLanguage) ?? primaries[0] ?? names[0];
}

/** The label of `jsonld` in `labelLanguage`, or `null` when it has no usable name. */
export function deriveLabel(jsonld: JsonObject, labelLanguage: string): string | null {
  const name = primaryName(usableNames(jsonld), labelLanguage);
  return name === undefined ? null : normalizeLabel(name.content);
}

/**
 * `jsonld` with its root `_label` set to the derived label: `jsonld` itself when there is no
 * usable name or the label is already that string, otherwise a shallow copy. The argument is
 * never modified.
 */
export function withDerivedLabel(jsonld: JsonObject, labelLanguage: string): JsonObject {
  const label = deriveLabel(jsonld, labelLanguage);
  if (label === null || own(jsonld, LABEL_KEY) === label) {
    return jsonld;
  }
  return { ...jsonld, [LABEL_KEY]: label };
}

/**
 * The live draft as JSON-LD: `project(doc)` with the derived root `_label`. Every production
 * reading of the draft as a document uses this, never `project` alone. It reads the document
 * only: no transaction, no update event, no undo entry.
 */
export function projectDraft(doc: Y.Doc, labelLanguage: string): JsonObject {
  return withDerivedLabel(project(doc), labelLanguage);
}
