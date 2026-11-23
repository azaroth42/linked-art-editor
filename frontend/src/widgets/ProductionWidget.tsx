import { useTranslation } from "react-i18next";

import type { ProfileSlot } from "../api/hooks/editing.ts";
import { addNode, removeEntry, type YMap } from "../crdt/paths.ts";
import { EdtfWidget } from "./EdtfWidget.tsx";
import { EntityRefAutocomplete } from "./EntityRefAutocomplete.tsx";
import { useEntries, useSlotText, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { ENTRY, SMALL_BUTTON } from "./styles.ts";
import { type LangMap, PRODUCTION_NODE } from "./vocab.ts";

/** The sub-slots of one `Production`, with paths relative to `prefix`. */
function subSlots(
  slot: ProfileSlot,
  prefix: string,
  labels: Record<"carried_out_by" | "timespan" | "took_place_at", string>,
): ProfileSlot[] {
  const at = (key: string) => (prefix === "" ? key : `${prefix}.${key}`);
  const common = { multiple: true, required: false, advanced: false } as const;
  const label = (text: string): LangMap => ({ en: text });
  return [
    {
      ...common,
      id: `${slot.id}_carried_out_by`,
      label: label(labels.carried_out_by),
      path: at("carried_out_by"),
      widget: "entity_ref",
      allowed_classes: ["Person", "Group"],
    },
    {
      ...common,
      id: `${slot.id}_timespan`,
      label: label(labels.timespan),
      path: at("timespan"),
      widget: "edtf",
      multiple: false,
    },
    {
      ...common,
      id: `${slot.id}_took_place_at`,
      label: label(labels.took_place_at),
      path: at("took_place_at"),
      widget: "entity_ref",
      allowed_classes: ["Place"],
    },
  ];
}

/** The sub-widgets of one production. */
function fields(slots: ProfileSlot[], base: YMap | null) {
  return slots.map((sub) =>
    sub.widget === "edtf" ? (
      <EdtfWidget key={sub.id} slot={sub} base={base} />
    ) : (
      <EntityRefAutocomplete key={sub.id} slot={sub} base={base} />
    ),
  );
}

/**
 * `production`: a `Production` (`produced_by`, single) or several (`produced_by.part[Production]`):
 * who carried it out (Person or Group references), when (EDTF), and where (Place references).
 * Its other properties (`technique`, `classified_as`) stay in the record untouched.
 */
export function ProductionWidget({ slot, base }: WidgetProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const { label, help } = useSlotText(slot);
  const entries = useEntries(env.doc, slot.path, base);
  const labels = (name: string) => ({
    carried_out_by: t("widgets.production.carried_out_by", { name }),
    timespan: t("widgets.production.timespan", { name }),
    took_place_at: t("widgets.production.took_place_at", { name }),
  });

  if (!slot.multiple) {
    // A single production: the sub-paths reach through it, creating it on the first write.
    return (
      <SlotFieldset label={label} help={help} required={slot.required}>
        {fields(subSlots(slot, slot.path, labels(label)), base)}
      </SlotFieldset>
    );
  }
  return (
    <SlotFieldset
      label={label}
      help={help}
      required={slot.required}
      onAdd={() =>
        addNode(env.doc, slot.path, { ...PRODUCTION_NODE }, { base, origin: env.origin })
      }
      addDisabled={env.disabled}
      empty={entries.length === 0}
    >
      {entries.map(([key, entry], index) => {
        const name = t("widgets.entry", { label, number: index + 1 });
        return (
          <div key={key} className={ENTRY}>
            {fields(subSlots(slot, "", labels(name)), entry)}
            <div>
              <button
                type="button"
                className={SMALL_BUTTON}
                disabled={env.disabled}
                onClick={() => removeEntry(env.doc, slot.path, key, { base, origin: env.origin })}
              >
                {t("widgets.remove", { name })}
              </button>
            </div>
          </div>
        );
      })}
    </SlotFieldset>
  );
}
