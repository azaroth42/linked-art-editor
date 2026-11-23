import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import type { BundleVocabulary } from "../api/hooks/editing.ts";
import { removeValue, setScalar, type YMap } from "../crdt/paths.ts";
import { useValue, useWidgetEnv } from "./environment.ts";
import { ENTRY, ERROR, FIELD, INPUT, LABEL, ROW, SMALL_BUTTON } from "./styles.ts";
import { TermSelect } from "./TermSelect.tsx";
import { TYPE_FIELD, UNIT_FIELD } from "./vocab.ts";

interface DimensionEntryProps {
  entry: YMap;
  /** The entry's accessible name, e.g. "Dimensions 1". */
  name: string;
  typeVocabulary: BundleVocabulary | null;
  unitVocabulary: BundleVocabulary | null;
  onRemove: () => void;
}

/**
 * One `Dimension`: its numeric `value`, `unit` and type. The value is written once it reads as
 * a finite number (typing "1." keeps the stored value until it does); clearing removes it.
 */
export function DimensionEntry({
  entry,
  name,
  typeVocabulary,
  unitVocabulary,
  onRemove,
}: DimensionEntryProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const valueId = useId();
  const errorId = useId();
  const stored = useValue(env.doc, "value", entry);
  const storedText = typeof stored === "number" ? String(stored) : "";
  const [text, setText] = useState(storedText);
  // Follow changes made elsewhere (another editor, undo) unless the field already says the same.
  useEffect(() => {
    setText((current) => (Number(current) === stored && current !== "" ? current : storedText));
  }, [stored, storedText]);
  const invalid = text.trim() !== "" && !Number.isFinite(Number(text));

  const onChange = (next: string) => {
    setText(next);
    const number = Number(next);
    if (next.trim() === "") {
      removeValue(env.doc, "value", { base: entry, origin: env.origin });
    } else if (Number.isFinite(number)) {
      setScalar(env.doc, "value", number, { base: entry, origin: env.origin });
    }
  };

  return (
    <div className={ENTRY}>
      <div className={ROW}>
        <TermSelect
          label={t("widgets.text.type", { name })}
          vocabulary={typeVocabulary}
          base={entry}
          {...TYPE_FIELD}
        />
        <div className={FIELD}>
          <label htmlFor={valueId} className={LABEL}>
            {t("widgets.dimension.value", { name })}
          </label>
          <input
            id={valueId}
            type="text"
            inputMode="decimal"
            className={INPUT}
            value={text}
            disabled={env.disabled}
            aria-invalid={invalid}
            aria-describedby={invalid ? errorId : undefined}
            onChange={(event) => onChange(event.target.value)}
          />
          {invalid && (
            <p id={errorId} className={ERROR}>
              {t("widgets.dimension.not_a_number")}
            </p>
          )}
        </div>
        <TermSelect
          label={t("widgets.dimension.unit", { name })}
          vocabulary={unitVocabulary}
          base={entry}
          {...UNIT_FIELD}
        />
        <button type="button" className={SMALL_BUTTON} disabled={env.disabled} onClick={onRemove}>
          {t("widgets.remove", { name })}
        </button>
      </div>
    </div>
  );
}
