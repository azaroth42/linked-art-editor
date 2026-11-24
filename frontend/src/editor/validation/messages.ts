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

/** The slot a missing root `_label` sends the user to: the record's names (Step 1.19a). */
export const NAME_SLOT_PATH = "identified_by[Name]";

/**
 * Whether a diagnostic is the schema's missing root `_label` (`LAE-SCHEMA-required` at `""` with
 * `missing` exactly `["_label"]`). Since Step 1.19a the label is derived from the record's
 * names, so the drawer asks for a name instead (`editor:validation.missing_label`) and jumps to
 * the first {@link NAME_SLOT_PATH} slot; code, path and params are unchanged.
 */
export function isMissingRootLabel(diagnostic: {
  code: string;
  path: string;
  params: Record<string, unknown>;
}): boolean {
  const missing = diagnostic.params.missing;
  return (
    diagnostic.code === "LAE-SCHEMA-required" &&
    diagnostic.path === "" &&
    Array.isArray(missing) &&
    missing.length === 1 &&
    missing[0] === "_label"
  );
}
