/**
 * Reading the Value Set bundle (`GET /api/vocab/bundle`) and LangMaps for the widgets.
 *
 * A LangMap (`profiles/README.md`) is resolved for the UI language, then its base language, then
 * `en`. A reference is labelled by its own `_label` (unresolved references and references added
 * in this session), else the label the server transcluded when the record was loaded, else the
 * label of a Value Set term with that URI, else the URI itself.
 */
import type { BundleTerm, BundleVocabulary, VocabBundle } from "../api/hooks/editing.ts";

export type LangMap = Record<string, string>;

/** The string of `map` for `language` (then its base language, then `en`, then any). */
export function localized(map: LangMap | null | undefined, language: string): string {
  if (map === null || map === undefined) {
    return "";
  }
  const base = language.split("-")[0] ?? language;
  return map[language] ?? map[base] ?? map.en ?? Object.values(map)[0] ?? "";
}

/** The Value Set with `id`, or `null`. */
export function findVocabulary(
  bundle: VocabBundle | null,
  id: string | null | undefined,
): BundleVocabulary | null {
  if (bundle === null || id === null || id === undefined) {
    return null;
  }
  return bundle.vocabularies.find((vocabulary) => vocabulary.id === id) ?? null;
}

/** Ids of the Value Sets bound to `(recordClass, path)`, in bundle order. */
export function boundVocabularyIds(
  bundle: VocabBundle | null,
  recordClass: string,
  path: string,
): string[] {
  if (bundle === null) {
    return [];
  }
  return bundle.bindings
    .filter((binding) => binding.entity_class === recordClass && binding.property_path === path)
    .map((binding) => binding.vocabulary_id);
}

/**
 * The Value Set for a sub-field of the nodes a slot edits (`classified_as`, `unit`): the slot's
 * own `value_set` for `classified_as`, else the first binding of `<slot path>.<key>`.
 */
export function subFieldVocabulary(
  bundle: VocabBundle | null,
  recordClass: string,
  slotPath: string,
  key: string,
  valueSet?: string | null,
): BundleVocabulary | null {
  if (key === "classified_as" && valueSet !== undefined && valueSet !== null) {
    return findVocabulary(bundle, valueSet);
  }
  const [first] = boundVocabularyIds(bundle, recordClass, `${slotPath}.${key}`);
  return findVocabulary(bundle, first);
}

/** The terms a picker offers: not deprecated, in Value Set order. */
export function offeredTerms(vocabulary: BundleVocabulary | null): BundleTerm[] {
  return vocabulary === null ? [] : vocabulary.terms.filter((term) => !term.deprecated);
}

/** `TermSelect` props for a node's type: its `classified_as` references of class `Type`. */
export const TYPE_FIELD = { property: "classified_as", referenceType: "Type" } as const;

/** `TermSelect` props for a dimension's `unit`: one `MeasurementUnit` reference. */
export const UNIT_FIELD = {
  property: "unit",
  referenceType: "MeasurementUnit",
  singleValued: true,
} as const;

/** A new `Production` node (`ProductionWidget`). */
export const PRODUCTION_NODE = { type: "Production" } as const;

/** `{classified_as: [default term]}` for a new node typed by `vocabulary`, or `{}`. */
export function defaultClassification(vocabulary: BundleVocabulary | null): {
  classified_as?: { id: string; type: string }[];
} {
  const preferred = vocabulary?.terms.find((term) => term.is_default && !term.deprecated);
  return preferred === undefined ? {} : { classified_as: [{ id: preferred.uri, type: "Type" }] };
}

/** The label of a Value Set term in `language`. */
export function termLabel(term: BundleTerm, language: string): string {
  return localized(term.label, language) || term.default_label;
}

/** The term of any Value Set in the bundle whose URI is `uri`. */
export function findTerm(bundle: VocabBundle | null, uri: string): BundleTerm | null {
  for (const vocabulary of bundle?.vocabularies ?? []) {
    const term = vocabulary.terms.find((candidate) => candidate.uri === uri);
    if (term !== undefined) {
      return term;
    }
  }
  return null;
}

/** A reference as the widgets read it from the projected JSON. */
export interface Reference {
  id: string;
  type: string | null;
  label: string | null;
  /** `id`s of the reference's own `classified_as` (inline metatypes, ADR-0032). */
  metatypes: string[];
}

function asArray(value: unknown): unknown[] {
  if (value === undefined || value === null) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
}

/** Read a reference (`{id, type, _label, classified_as}`) from JSON; `null` if it has no `id`. */
export function readReference(json: unknown): Reference | null {
  if (typeof json !== "object" || json === null) {
    return null;
  }
  const object = json as Record<string, unknown>;
  if (typeof object.id !== "string") {
    return null;
  }
  return {
    id: object.id,
    type: typeof object.type === "string" ? object.type : null,
    label: typeof object._label === "string" ? object._label : null,
    metatypes: asArray(object.classified_as)
      .map((entry) => readReference(entry)?.id)
      .filter((id): id is string => id !== undefined),
  };
}

/** The label to show for `reference` (see the module comment). */
export function referenceLabel(
  reference: Reference,
  bundle: VocabBundle | null,
  refLabels: ReadonlyMap<string, string>,
  language: string,
): string {
  if (reference.label !== null && reference.label !== "") {
    return reference.label;
  }
  const loaded = refLabels.get(reference.id);
  if (loaded !== undefined) {
    return loaded;
  }
  const term = findTerm(bundle, reference.id);
  return term === null ? reference.id : termLabel(term, language);
}

/** The `_label` of every reference in a JSON-LD record, by `id` (the server's transcluded labels). */
export function collectReferenceLabels(jsonld: unknown): Map<string, string> {
  const labels = new Map<string, string>();
  const visit = (value: unknown, isRoot: boolean) => {
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, false));
      return;
    }
    if (typeof value !== "object" || value === null) {
      return;
    }
    const object = value as Record<string, unknown>;
    if (!isRoot && typeof object.id === "string" && typeof object._label === "string") {
      labels.set(object.id, object._label);
    }
    for (const [key, child] of Object.entries(object)) {
      if (key !== "@context") {
        visit(child, false);
      }
    }
  };
  visit(jsonld, true);
  return labels;
}
