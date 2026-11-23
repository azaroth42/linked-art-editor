import { useId } from "react";
import { useTranslation } from "react-i18next";

import type { BundleVocabulary } from "../api/hooks/editing.ts";
import type { YMap } from "../crdt/paths.ts";
import { useYPath } from "../crdt/useYPath.ts";
import { claims } from "./classifications.ts";
import { replaceReferences, setReference } from "./edits.ts";
import { useLanguage, useWidgetEnv } from "./environment.ts";
import { FIELD, LABEL, SELECT } from "./styles.ts";
import { offeredTerms, readReference, referenceLabel, termLabel } from "./vocab.ts";

interface TermSelectProps {
  /** The control's accessible label. */
  label: string;
  vocabulary: BundleVocabulary | null;
  /** The node whose `property` is edited. */
  base: YMap | null;
  /** `classified_as` (an array; only the references the Value Set claims are replaced) or `unit`. */
  property: string;
  /** The `type` of a reference this select writes (`Type`, `MeasurementUnit`). */
  referenceType: string;
  /** The property holds one reference (`unit`) rather than an array. */
  singleValued?: boolean;
}

/**
 * A select of the terms of one Value Set, bound to `property` of a node: the type of a name,
 * identifier, statement or dimension (`classified_as`), or a dimension's `unit`. Of an array
 * only the reference the Value Set claims is shown and replaced; every other value stays.
 */
export function TermSelect({
  label,
  vocabulary,
  base,
  property,
  referenceType,
  singleValued = false,
}: TermSelectProps) {
  const { t } = useTranslation("editor");
  const id = useId();
  const env = useWidgetEnv();
  const language = useLanguage();
  const { json } = useYPath(env.doc, property, { base });
  if (vocabulary === null) {
    return null;
  }
  const references = json
    .map((value) => readReference(value))
    .filter((reference) => reference !== null);
  const current = singleValued
    ? (references[0] ?? null)
    : (references.find((reference) => claims(vocabulary, reference)) ?? null);
  const terms = offeredTerms(vocabulary);
  const extra = current !== null && !terms.some((term) => term.uri === current.id);

  const onChange = (uri: string) => {
    const next = uri === "" ? null : { id: uri, type: referenceType };
    if (singleValued) {
      setReference(env.doc, property, next, { base, origin: env.origin });
    } else {
      replaceReferences(env.doc, property, {
        base,
        origin: env.origin,
        remove: (reference) => claims(vocabulary, reference),
        add: next,
      });
    }
  };

  return (
    <div className={FIELD}>
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      <select
        id={id}
        className={SELECT}
        value={current?.id ?? ""}
        disabled={env.disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{t("widgets.term.none")}</option>
        {extra && (
          <option value={current.id}>
            {referenceLabel(current, env.bundle, env.refLabels, language)}
          </option>
        )}
        {terms.map((term) => (
          <option key={term.uri} value={term.uri}>
            {termLabel(term, language)}
          </option>
        ))}
      </select>
    </div>
  );
}
