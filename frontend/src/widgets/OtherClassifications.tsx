import { useTranslation } from "react-i18next";

import type { YMap } from "../crdt/paths.ts";
import type { KeyedReference } from "./ClassificationCard.tsx";
import { removeReference } from "./edits.ts";
import { useLanguage, useWidgetEnv } from "./environment.ts";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { SMALL_BUTTON } from "./styles.ts";
import { referenceLabel } from "./vocab.ts";

/**
 * "Other classifications" (decision C5): values at a classification path that no card's Value
 * Set claims. They are shown so nothing is hidden or dropped, and can only be removed.
 */
export function OtherClassifications({
  path,
  base,
  values,
}: {
  path: string;
  base: YMap | null;
  values: KeyedReference[];
}) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const language = useLanguage();
  return (
    <SlotFieldset
      label={t("widgets.classification.other")}
      help={t("widgets.classification.other_help")}
      required={false}
    >
      <ul className="flex flex-col gap-1">
        {values.map((value) => {
          const name = referenceLabel(value, env.bundle, env.refLabels, language);
          return (
            <li key={value.key ?? value.id} className="flex flex-wrap items-center gap-2">
              <span className="text-slate-900">{name}</span>
              <button
                type="button"
                className={SMALL_BUTTON}
                disabled={env.disabled}
                onClick={() =>
                  removeReference(env.doc, path, value.key, { base, origin: env.origin })
                }
              >
                {t("widgets.remove", { name })}
              </button>
            </li>
          );
        })}
      </ul>
    </SlotFieldset>
  );
}
