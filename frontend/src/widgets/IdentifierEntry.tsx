import { useId, useRef } from "react";
import { useTranslation } from "react-i18next";

import type { BundleVocabulary } from "../api/hooks/editing.ts";
import { setScalar, type YMap } from "../crdt/paths.ts";
import { useValue, useWidgetEnv } from "./environment.ts";
import { ENTRY, FIELD, INPUT, LABEL, ROW, SMALL_BUTTON } from "./styles.ts";
import { TermSelect } from "./TermSelect.tsx";
import { useBoundText } from "./useBoundText.ts";
import { TYPE_FIELD } from "./vocab.ts";

interface IdentifierEntryProps {
  entry: YMap;
  /** The entry's accessible name, e.g. "Identifiers 1". */
  name: string;
  typeVocabulary: BundleVocabulary | null;
  onRemove: () => void;
}

/** One `Identifier`: its `content` and its type. */
export function IdentifierEntry({ entry, name, typeVocabulary, onRemove }: IdentifierEntryProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const id = useId();
  const content = useValue(env.doc, "content", entry);
  const text = typeof content === "string" ? content : "";
  const field = useRef<HTMLInputElement>(null);
  useBoundText(field, text);
  return (
    <div className={ENTRY}>
      <div className={ROW}>
        <div className={FIELD}>
          <label htmlFor={id} className={LABEL}>
            {name}
          </label>
          <input
            id={id}
            type="text"
            ref={field}
            className={INPUT}
            defaultValue={text}
            disabled={env.disabled}
            onChange={(event) =>
              setScalar(env.doc, "content", event.target.value, {
                base: entry,
                origin: env.origin,
              })
            }
          />
        </div>
        <TermSelect
          label={t("widgets.text.type", { name })}
          vocabulary={typeVocabulary}
          base={entry}
          {...TYPE_FIELD}
        />
        <button type="button" className={SMALL_BUTTON} disabled={env.disabled} onClick={onRemove}>
          {t("widgets.remove", { name })}
        </button>
      </div>
    </div>
  );
}
