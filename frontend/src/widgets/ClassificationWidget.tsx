import { useMemo } from "react";

import { useYPath } from "../crdt/useYPath.ts";
import { ClassificationCard, type KeyedReference } from "./ClassificationCard.tsx";
import { partitionClassifications } from "./classifications.ts";
import { referencesAt } from "./edits.ts";
import { useLanguage, useSlotText, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { OtherClassifications } from "./OtherClassifications.tsx";
import { findVocabulary, localized } from "./vocab.ts";

/**
 * `classification`: the references at the slot path that the slot's Value Set claims, as pills
 * (`classifications.ts` says which card shows which value). The path's last classification slot
 * also renders a card for each Value Set the bundle binds to the path without a slot, and the
 * "Other classifications" group for values no card claims.
 */
export function ClassificationWidget({ slot, base }: WidgetProps) {
  const env = useWidgetEnv();
  const language = useLanguage();
  const { label, help } = useSlotText(slot);
  const { json } = useYPath(env.doc, slot.path, { base });
  const values = useMemo<KeyedReference[]>(
    () =>
      json.length === 0
        ? []
        : referencesAt(env.doc, slot.path, base).map(({ key, reference }) => ({
            ...reference,
            key,
          })),
    [env.doc, slot.path, base, json],
  );
  const vocabulary = findVocabulary(env.bundle, slot.value_set);
  const group = env.classificationGroups.get(slot.path);
  const cards = group?.vocabularies ?? (vocabulary === null ? [] : [vocabulary]);
  const partition = partitionClassifications(values, cards);
  const owner = group === undefined || group.ownerSlotId === slot.id;

  return (
    <>
      {vocabulary !== null && (
        <ClassificationCard
          label={label}
          help={help}
          required={slot.required}
          path={slot.path}
          base={base}
          vocabulary={vocabulary}
          values={partition.claimed.get(vocabulary.id) ?? []}
        />
      )}
      {owner &&
        (group?.unslotted ?? []).map((extra) => (
          <ClassificationCard
            key={extra.id}
            label={localized(extra.label, language)}
            help={localized(extra.scope_note ?? null, language)}
            required={false}
            path={slot.path}
            base={base}
            vocabulary={extra}
            values={partition.claimed.get(extra.id) ?? []}
          />
        ))}
      {owner && partition.other.length > 0 && (
        <OtherClassifications path={slot.path} base={base} values={partition.other} />
      )}
    </>
  );
}
