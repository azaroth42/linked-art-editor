import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import {
  type DateInput,
  formatDate,
  formatList,
  formatNumber,
  formatRelative,
  intlLocale,
} from "./formatters.ts";

/** The `Intl` formatters of `formatters.ts`, bound to the active UI language. */
export function useFormatters() {
  const { i18n } = useTranslation();
  const locale = intlLocale(i18n.resolvedLanguage ?? i18n.language);
  return useMemo(
    () => ({
      locale,
      formatDate: (value: DateInput, options?: Intl.DateTimeFormatOptions) =>
        formatDate(locale, value, options),
      formatRelative: (value: DateInput, now?: DateInput) => formatRelative(locale, value, now),
      formatNumber: (value: number | bigint, options?: Intl.NumberFormatOptions) =>
        formatNumber(locale, value, options),
      formatList: (items: Iterable<string>, options?: Intl.ListFormatOptions) =>
        formatList(locale, items, options),
    }),
    [locale],
  );
}
