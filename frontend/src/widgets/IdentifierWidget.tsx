import { useTranslation } from "react-i18next";

import { addNode, removeEntry } from "../crdt/paths.ts";
import { useEntries, useSlotText, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { IdentifierEntry } from "./IdentifierEntry.tsx";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { defaultClassification, subFieldVocabulary } from "./vocab.ts";

/** `identifier`: `Identifier` nodes, each with its `content` and its type from the Value Set. */
export function IdentifierWidget({ slot, base }: WidgetProps) {
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
        <IdentifierEntry
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
