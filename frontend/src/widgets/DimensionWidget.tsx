import { useTranslation } from "react-i18next";

import { addNode, removeEntry } from "../crdt/paths.ts";
import { DimensionEntry } from "./DimensionEntry.tsx";
import { useEntries, useSlotText, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { subFieldVocabulary } from "./vocab.ts";

/**
 * `dimension`: `Dimension` nodes, each with a value, a unit (the Value Set bound to
 * `<slot path>.unit`) and a type (the slot's Value Set).
 */
export function DimensionWidget({ slot, base }: WidgetProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const { label, help } = useSlotText(slot);
  const entries = useEntries(env.doc, slot.path, base);
  const typeVocabulary = subFieldVocabulary(
    env.bundle,
    env.recordClass,
    slot.path,
    "classified_as",
    slot.value_set,
  );
  const unitVocabulary = subFieldVocabulary(env.bundle, env.recordClass, slot.path, "unit");
  const add = () =>
    addNode(env.doc, slot.path, { type: "Dimension" }, { base, origin: env.origin });
  return (
    <SlotFieldset
      label={label}
      help={help}
      required={slot.required}
      onAdd={slot.multiple || entries.length === 0 ? add : null}
      addDisabled={env.disabled}
      empty={entries.length === 0}
    >
      {entries.map(([key, entry], index) => (
        <DimensionEntry
          key={key}
          entry={entry}
          name={t("widgets.entry", { label, number: index + 1 })}
          typeVocabulary={typeVocabulary}
          unitVocabulary={unitVocabulary}
          onRemove={() => removeEntry(env.doc, slot.path, key, { base, origin: env.origin })}
        />
      ))}
    </SlotFieldset>
  );
}
