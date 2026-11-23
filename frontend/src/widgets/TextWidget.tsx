import { useId } from "react";
import { useTranslation } from "react-i18next";

import { removeValue, setScalar } from "../crdt/paths.ts";
import { useSlotText, useValue, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { FIELD, HELP, INPUT, LABEL } from "./styles.ts";

/**
 * `text` (and, until it has its own widget, `wkt`): the one string literal the slot path
 * addresses, a last-writer-wins value. Clearing the field removes the property.
 */
export function TextWidget({ slot, base }: WidgetProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const id = useId();
  const helpId = useId();
  const { label, help } = useSlotText(slot);
  const value = useValue(env.doc, slot.path, base);

  const onChange = (next: string) => {
    if (next === "") {
      removeValue(env.doc, slot.path, { base, origin: env.origin });
    } else {
      setScalar(env.doc, slot.path, next, { base, origin: env.origin });
    }
  };

  return (
    <div className={FIELD}>
      <label htmlFor={id} className={LABEL}>
        {slot.required ? t("widgets.required_label", { label }) : label}
      </label>
      <input
        id={id}
        type="text"
        className={INPUT}
        value={typeof value === "string" ? value : ""}
        disabled={env.disabled}
        aria-describedby={help === "" ? undefined : helpId}
        onChange={(event) => onChange(event.target.value)}
      />
      {help !== "" && (
        <p id={helpId} className={HELP}>
          {help}
        </p>
      )}
    </div>
  );
}
