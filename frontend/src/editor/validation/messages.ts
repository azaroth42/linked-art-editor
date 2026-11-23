/**
 * Localized diagnostic messages (decision C3): the text is the `errors` namespace entry of the
 * diagnostic's code, an ICU message whose placeholders are filled from `params`. Parameters are
 * JSON values; ICU interpolates strings and numbers, so lists become a localized list
 * ("a, b, and c") and other values their JSON text (`message_en` on the server does the same
 * with ", ").
 */
import { formatList } from "../../i18n/formatters.ts";

function text(value: unknown, locale: string): string {
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return formatList(
      locale,
      value.map((item: unknown) => text(item, locale)),
    );
  }
  return JSON.stringify(value) ?? "";
}

/** `params` made ready for ICU: numbers and strings as they are, anything else as text. */
export function messageParams(
  params: Record<string, unknown>,
  locale: string,
): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(params)) {
    out[key] = typeof value === "number" ? value : text(value, locale);
  }
  return out;
}
