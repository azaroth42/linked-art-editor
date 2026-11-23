import { useId, useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { YMap } from "../crdt/paths.ts";
import { useYPath } from "../crdt/useYPath.ts";
import { intlLocale } from "../i18n/formatters.ts";
import { replaceReferences } from "./edits.ts";
import { useLanguage, useWidgetEnv } from "./environment.ts";
import { LANGUAGE_TAGS, LANGUAGE_URIS } from "./languages.ts";
import { FIELD, LABEL, SELECT } from "./styles.ts";
import { readReference, referenceLabel } from "./vocab.ts";

/**
 * The language of a text node (`language`, references to AAT language concepts) as a select of
 * BCP-47 tags named with `Intl.DisplayNames`. Choosing a language replaces every `language`
 * reference with that one concept; a language outside the table is shown and kept until another
 * is chosen.
 */
export function LanguageSelect({ label, base }: { label: string; base: YMap | null }) {
  const { t } = useTranslation("editor");
  const id = useId();
  const env = useWidgetEnv();
  const language = useLanguage();
  const { json } = useYPath(env.doc, "language", { base });
  const options = useMemo(() => {
    const names = new Intl.DisplayNames([intlLocale(language)], { type: "language" });
    return [...LANGUAGE_URIS.keys()]
      .map((tag) => ({ tag, name: names.of(tag) ?? tag }))
      .sort((a, b) => a.name.localeCompare(b.name, intlLocale(language)));
  }, [language]);

  const current = json.map((value) => readReference(value)).find((value) => value !== null);
  const tag = current === undefined ? "" : (LANGUAGE_TAGS.get(current.id) ?? current.id);

  const onChange = (value: string) => {
    const uri = LANGUAGE_URIS.get(value);
    replaceReferences(env.doc, "language", {
      base,
      origin: env.origin,
      remove: () => true,
      add: uri === undefined ? null : { id: uri, type: "Language" },
    });
  };

  return (
    <div className={FIELD}>
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      <select
        id={id}
        className={SELECT}
        value={tag}
        disabled={env.disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{t("widgets.language.none")}</option>
        {current !== undefined && !LANGUAGE_TAGS.has(current.id) && (
          <option value={current.id}>
            {referenceLabel(current, env.bundle, env.refLabels, language)}
          </option>
        )}
        {options.map(({ tag: value, name }) => (
          <option key={value} value={value}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}
