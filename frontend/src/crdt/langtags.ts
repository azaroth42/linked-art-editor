/**
 * The language of a Linked Art `language` entry, as the server reads it: the TypeScript mirror
 * of what `deriveLabel` needs of `backend/src/lae/labels/langtags.py` (Step 1.19a).
 *
 * {@link primaryLanguageOf} equals Python's `primary_language(language_tag(entry))` for every
 * JSON value: the `notation`, if it is a well-formed tag once stripped, decides alone (its first
 * subtag, lower case, shortened through {@link ISO_639_2_TO_1}); otherwise the `id`, if it is an
 * AAT language concept of the table (`AAT_LANGUAGE_TAGS` in `widgets/languages.ts`, the
 * frontend's only copy of it); otherwise `"und"`. The canonical form of the whole tag is not
 * needed and not mirrored. `derived_label_cases.json` proves both tables equal to Python's.
 */
import { AAT_LANGUAGE_TAGS } from "../widgets/languages.ts";
import { isObject, own } from "./rules.ts";

/** The tag of a language that is not stated or not recognised. */
export const UNDETERMINED = "und";

/**
 * Python's whitespace (`str.isspace`, `str.strip`, `str.split`): exactly these 29 code points,
 * as the body of a character class. JavaScript's `\s` and `trim()` use another set (they include
 * U+FEFF and exclude U+001C–U+001F and U+0085), so they are never used here.
 */
export const WHITESPACE_CLASS =
  "\\u0009-\\u000D\\u001C-\\u001F\\u0020\\u0085\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000";

const EDGE_WHITESPACE = new RegExp(`^[${WHITESPACE_CLASS}]+|[${WHITESPACE_CLASS}]+$`, "gu");

/** `text` without leading and trailing whitespace: Python's `str.strip()`. */
export function stripWhitespace(text: string): string {
  return text.replace(EDGE_WHITESPACE, "");
}

/** The AAT namespace spellings accepted for concept URIs (Python's `AAT_PREFIXES`). */
export const AAT_PREFIXES: readonly string[] = [
  "http://vocab.getty.edu/aat/",
  "https://vocab.getty.edu/aat/",
  "aat:",
];

/**
 * ISO 639-2 (bibliographic and terminology) codes of the table's languages → their ISO 639-1
 * code: Python's `ISO_639_2_TO_1`.
 */
// prettier-ignore
export const ISO_639_2_TO_1: ReadonlyMap<string, string> = new Map(
  Object.entries({
    afr: "af", alb: "sq", sqi: "sq", ara: "ar", arm: "hy", hye: "hy", baq: "eu", eus: "eu",
    bel: "be", ben: "bn", bul: "bg", cat: "ca", chi: "zh", zho: "zh", hrv: "hr", cze: "cs",
    ces: "cs", dan: "da", dut: "nl", nld: "nl", eng: "en", epo: "eo", est: "et", fin: "fi",
    fre: "fr", fra: "fr", glg: "gl", geo: "ka", kat: "ka", ger: "de", deu: "de", heb: "he",
    hin: "hi", ice: "is", isl: "is", ind: "id", ita: "it", jpn: "ja", kor: "ko", lat: "la",
    lav: "lv", lit: "lt", mac: "mk", mkd: "mk", mlt: "mt", nno: "nn", pol: "pl", por: "pt",
    rum: "ro", ron: "ro", rus: "ru", san: "sa", srp: "sr", slo: "sk", slk: "sk", slv: "sl",
    spa: "es", swe: "sv", tam: "ta", tha: "th", tib: "bo", bod: "bo", tur: "tr", ukr: "uk",
    urd: "ur", vie: "vi", wel: "cy", cym: "cy", yid: "yi", gre: "el", ell: "el", nob: "nb",
    oci: "oc",
  }),
);

/** A well-formed tag (Python's `_TAG_RE`, matched in full). */
const TAG = /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$/;

/** The numeric id of an AAT URI in one of the three spellings (ASCII digits only), else `null`. */
export function aatId(uri: string): string | null {
  for (const prefix of AAT_PREFIXES) {
    if (uri.startsWith(prefix)) {
      const local = uri.slice(prefix.length);
      return /^[0-9]+$/.test(local) ? local : null;
    }
  }
  return null;
}

/** The first subtag of a tag, lower case (Python's `primary_language`). */
function primaryLanguage(tag: string): string {
  return (tag.split("-")[0] ?? "").toLowerCase();
}

/**
 * The primary language of one `language` entry: Python's
 * `primary_language(language_tag(entry))` (`"en"` for a notation `en-GB` or `eng`, or the AAT
 * English concept without one; `"und"` when neither says).
 */
export function primaryLanguageOf(entry: unknown): string {
  if (!isObject(entry)) {
    return UNDETERMINED;
  }
  const notation = own(entry, "notation");
  if (typeof notation === "string") {
    const stripped = stripWhitespace(notation);
    if (TAG.test(stripped)) {
      // `normalize_tag` shortens the language subtag through ISO_639_2_TO_1.
      const language = primaryLanguage(stripped);
      return ISO_639_2_TO_1.get(language) ?? language;
    }
  }
  const id = own(entry, "id");
  if (typeof id === "string") {
    const concept = aatId(id);
    const tag = concept === null ? undefined : AAT_LANGUAGE_TAGS.get(concept);
    if (tag !== undefined) {
      return primaryLanguage(tag);
    }
  }
  return UNDETERMINED;
}
