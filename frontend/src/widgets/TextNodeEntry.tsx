import { useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { BundleVocabulary } from "../api/hooks/editing.ts";
import { setScalar, type YMap } from "../crdt/paths.ts";
import { useWidgetEnv, useValue } from "./environment.ts";
import { LanguageSelect } from "./LanguageSelect.tsx";
import { ENTRY, FIELD, LABEL, ROW, SELECT, SMALL_BUTTON, TEXTAREA } from "./styles.ts";
import { TermSelect } from "./TermSelect.tsx";
import { useBoundText } from "./useBoundText.ts";
import { TYPE_FIELD } from "./vocab.ts";

/** Text directions offered for display; `auto` lets the browser decide from the text. */
const DIRECTIONS = ["auto", "ltr", "rtl"] as const;
type Direction = (typeof DIRECTIONS)[number];

interface TextNodeEntryProps {
  /** The `Name` or `LinguisticObject` map. */
  entry: YMap;
  /** The entry's accessible name, e.g. "Title 2". */
  name: string;
  /** The Value Set of the node's `classified_as` (its type), if any. */
  typeVocabulary: BundleVocabulary | null;
  onRemove: () => void;
}

/**
 * One `Name` or `LinguisticObject`: its `content` (a `Y.Text`; each change is the one splice
 * that turns the old text into the new, so concurrent edits elsewhere in it survive), language,
 * type, and a display direction for the text area (`auto` by default; not stored).
 */
export function TextNodeEntry({ entry, name, typeVocabulary, onRemove }: TextNodeEntryProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const contentId = useId();
  const directionId = useId();
  const [direction, setDirection] = useState<Direction>("auto");
  const content = useValue(env.doc, "content", entry);
  const text = typeof content === "string" ? content : "";
  const field = useRef<HTMLTextAreaElement>(null);
  useBoundText(field, text);

  return (
    <div className={ENTRY}>
      <div className={FIELD}>
        <label htmlFor={contentId} className={LABEL}>
          {name}
        </label>
        <textarea
          id={contentId}
          className={TEXTAREA}
          dir={direction}
          ref={field}
          rows={2}
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
      <div className={ROW}>
        <LanguageSelect label={t("widgets.text.language", { name })} base={entry} />
        <div className={FIELD}>
          <label htmlFor={directionId} className={LABEL}>
            {t("widgets.text.direction", { name })}
          </label>
          <select
            id={directionId}
            className={SELECT}
            value={direction}
            onChange={(event) => setDirection(event.target.value as Direction)}
          >
            {DIRECTIONS.map((value) => (
              <option key={value} value={value}>
                {t(`widgets.text.direction_${value}`)}
              </option>
            ))}
          </select>
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
