import { type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";

import { HELP, SLOT, SLOT_LEGEND, SMALL_BUTTON } from "./styles.ts";

interface SlotFieldsetProps {
  label: string;
  help: string;
  required: boolean;
  /** Adds an entry; no button when `null`. */
  onAdd?: (() => void) | null;
  addDisabled?: boolean;
  /** Shown when there are no entries. */
  empty?: boolean;
  children: ReactNode;
}

/** A slot holding several controls or entries: a fieldset named by the slot label. */
export function SlotFieldset({
  label,
  help,
  required,
  onAdd = null,
  addDisabled = false,
  empty = false,
  children,
}: SlotFieldsetProps) {
  const { t } = useTranslation("editor");
  const helpId = useId();
  return (
    <fieldset className={SLOT} aria-describedby={help === "" ? undefined : helpId}>
      <legend className={SLOT_LEGEND}>
        {required ? t("widgets.required_label", { label }) : label}
      </legend>
      {help !== "" && (
        <p id={helpId} className={HELP}>
          {help}
        </p>
      )}
      {empty && <p className={HELP}>{t("widgets.empty")}</p>}
      {children}
      {onAdd !== null && (
        <div>
          <button type="button" className={SMALL_BUTTON} disabled={addDisabled} onClick={onAdd}>
            {t("widgets.add", { label })}
          </button>
        </div>
      )}
    </fieldset>
  );
}
