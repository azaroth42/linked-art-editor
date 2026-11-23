import { useTranslation } from "react-i18next";

import type { BundleVocabulary } from "../api/hooks/editing.ts";
import { addReference, type YMap } from "../crdt/paths.ts";
import { removeReference } from "./edits.ts";
import { useLanguage, useWidgetEnv } from "./environment.ts";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { PILL } from "./styles.ts";
import { offeredTerms, type Reference, referenceLabel, termLabel } from "./vocab.ts";

/** A value at the path with its entry key (`null` for a single value). */
export type KeyedReference = Reference & { key: string | null };

interface ClassificationCardProps {
  label: string;
  help: string;
  required: boolean;
  path: string;
  base: YMap | null;
  vocabulary: BundleVocabulary;
  /** The values this card's Value Set claims. */
  values: KeyedReference[];
}

/** The class a new reference of `vocabulary` gets: the rule's one class, else `Type`. */
function referenceClass(vocabulary: BundleVocabulary): string {
  const classes = vocabulary.rule?.classes ?? [];
  return classes.length === 1 && classes[0] !== undefined ? classes[0] : "Type";
}

/**
 * One Value Set's card: a toggle pill per offered term, pressed when the record has it, and a
 * pressed pill for every other value the set claims (a rule set's member that is not pinned).
 * Pressing adds the reference at the end; un-pressing removes it.
 */
export function ClassificationCard({
  label,
  help,
  required,
  path,
  base,
  vocabulary,
  values,
}: ClassificationCardProps) {
  const env = useWidgetEnv();
  const language = useLanguage();
  const { t } = useTranslation("editor");
  const terms = offeredTerms(vocabulary);
  const unlisted = values.filter((value) => !terms.some((term) => term.uri === value.id));
  const pills = [
    ...terms.map((term) => ({ id: term.uri, name: termLabel(term, language) })),
    ...unlisted.map((value) => ({
      id: value.id,
      name: referenceLabel(value, env.bundle, env.refLabels, language),
    })),
  ];

  const toggle = (id: string) => {
    const present = values.filter((value) => value.id === id);
    if (present.length === 0) {
      addReference(env.doc, path, id, {
        base,
        origin: env.origin,
        type: referenceClass(vocabulary),
      });
      return;
    }
    env.doc.transact(() => {
      for (const value of present) {
        removeReference(env.doc, path, value.key, { base, origin: env.origin });
      }
    }, env.origin);
  };

  return (
    <SlotFieldset label={label} help={help} required={required}>
      <ul
        className="flex flex-wrap gap-2"
        aria-label={t("widgets.classification.terms", { label })}
      >
        {pills.map((pill) => (
          <li key={pill.id}>
            <button
              type="button"
              className={PILL}
              aria-pressed={values.some((value) => value.id === pill.id)}
              disabled={env.disabled}
              onClick={() => toggle(pill.id)}
            >
              {pill.name}
            </button>
          </li>
        ))}
      </ul>
    </SlotFieldset>
  );
}
