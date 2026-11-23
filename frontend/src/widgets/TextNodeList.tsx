import { useTranslation } from "react-i18next";

import { addNode, removeEntry } from "../crdt/paths.ts";
import { useEntries, useSlotText, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { TextNodeEntry } from "./TextNodeEntry.tsx";
import { defaultClassification, subFieldVocabulary } from "./vocab.ts";

/**
 * The `Name` or `LinguisticObject` nodes a slot path addresses (`identified_by[Name]`,
 * `referred_to_by[LinguisticObject]`), one {@link TextNodeEntry} each. A new entry takes the
 * class of the path's filter and the type Value Set's default term.
 */
export function TextNodeList({ slot, base }: WidgetProps) {
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

  const add = () =>
    addNode(
      env.doc,
      slot.path,
      { content: "", ...defaultClassification(typeVocabulary) },
      { base, origin: env.origin },
    );

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
        <TextNodeEntry
          key={key}
          entry={entry}
          name={t("widgets.entry", { label, number: index + 1 })}
          typeVocabulary={typeVocabulary}
          onRemove={() => removeEntry(env.doc, slot.path, key, { base, origin: env.origin })}
        />
      ))}
    </SlotFieldset>
  );
}
