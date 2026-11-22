/**
 * Development pseudo-locale (spec §6.7): every translated string is wrapped in `[ … ]`, its ASCII
 * letters are swapped for accented look-alikes, and it is padded by ~35% so that unwrapped literals
 * and layout overflow are visible before any real translation exists.
 */
import type { PostProcessorModule, TOptions } from "i18next";

export const PSEUDO_LANGUAGE = "pseudo";
export const PSEUDO_POST_PROCESSOR = "pseudo";
export const PSEUDO_EXPANSION = 0.35;

const PLAIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
const ACCENTED = "áƀçðéƒĝĥíĵķļɱñóþǫŕšţúṽŵẋýžÅƁÇÐÉƑĜĤÍĴĶĻṀÑÓÞǪŔŠŢÚṼŴẊÝŽ";
const ACCENT_MAP = new Map(Array.from(PLAIN, (ch, i) => [ch, Array.from(ACCENTED)[i] ?? ch]));

/** About `length` characters of tildes in groups of five, so padded strings still wrap. */
function padding(length: number): string {
  return Array.from({ length }, (_, i) => (i % 6 === 5 ? " " : "~"))
    .join("")
    .trimEnd();
}

/**
 * Pseudo-localise one string. Text inside `{…}` (ICU arguments) and `<…>` (Trans component tags)
 * is left untouched so the result still renders.
 */
export function pseudoLocalize(text: string): string {
  let depth = 0;
  let out = "";
  for (const ch of text) {
    if (ch === "{" || ch === "<") {
      depth += 1;
    } else if ((ch === "}" || ch === ">") && depth > 0) {
      depth -= 1;
    }
    out += depth === 0 ? (ACCENT_MAP.get(ch) ?? ch) : ch;
  }
  return `[${out} ${padding(Math.ceil(text.length * PSEUDO_EXPANSION))}]`;
}

/** i18next post-processor; a no-op unless the active (or requested) language is `pseudo`. */
export const pseudoPostProcessor: PostProcessorModule = {
  type: "postProcessor",
  name: PSEUDO_POST_PROCESSOR,
  process(value: string, _key: string | string[], options: TOptions, translator: unknown): string {
    const language = options.lng ?? (translator as { language?: string } | undefined)?.language;
    return language === PSEUDO_LANGUAGE ? pseudoLocalize(value) : value;
  },
};
