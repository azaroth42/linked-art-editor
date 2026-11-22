/**
 * Locale-aware formatting through `Intl` (spec §6.7, conventions §5): dates, relative times,
 * numbers, and lists are never formatted by hand. Each function takes the BCP-47 locale to use;
 * components call them through `useFormatters`, which supplies the active UI language.
 * `Intl` objects are cached per locale and options because constructing them is expensive.
 */
import { DEFAULT_LANGUAGE } from "./index.ts";
import { PSEUDO_LANGUAGE } from "./pseudo.ts";

/** A date as a `Date`, an ISO 8601 string (the API's format), or epoch milliseconds. */
export type DateInput = Date | string | number;

/** The `Intl` locale for a UI language: the pseudo locale formats like {@link DEFAULT_LANGUAGE}. */
export function intlLocale(language: string): string {
  return language === PSEUDO_LANGUAGE || language === "" ? DEFAULT_LANGUAGE : language;
}

const cache = new Map<string, unknown>();

function cached<T>(kind: string, locale: string, options: object | undefined, create: () => T): T {
  const key = `${kind}\u0000${locale}\u0000${JSON.stringify(options ?? {})}`;
  let formatter = cache.get(key) as T | undefined;
  if (formatter === undefined) {
    formatter = create();
    cache.set(key, formatter);
  }
  return formatter;
}

function toDate(value: DateInput): Date {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) {
    throw new RangeError(`Invalid date: ${String(value)}`);
  }
  return date;
}

/** Format a date (default: medium date style, e.g. "22 Nov 2026" in `en-GB`). */
export function formatDate(
  locale: string,
  value: DateInput,
  options: Intl.DateTimeFormatOptions = { dateStyle: "medium" },
): string {
  const formatter = cached(
    "date",
    locale,
    options,
    () => new Intl.DateTimeFormat(intlLocale(locale), options),
  );
  return formatter.format(toDate(value));
}

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

/**
 * The largest unit in which a span reads naturally, with its length in milliseconds. Thresholds
 * round up a little early ("45 minutes" → "1 hour") as people say them.
 */
const UNITS: readonly (readonly [Intl.RelativeTimeFormatUnit, number, number])[] = [
  ["year", YEAR, 11 * MONTH],
  ["month", MONTH, 4 * WEEK],
  ["week", WEEK, 7 * DAY],
  ["day", DAY, 22 * HOUR],
  ["hour", HOUR, 45 * MINUTE],
  ["minute", MINUTE, 45 * SECOND],
  ["second", SECOND, 0],
];

/**
 * Format `value` relative to `now`, e.g. "yesterday", "in 5 days", "3 hours ago" (`numeric:
 * "auto"`), using the largest sensible unit.
 */
export function formatRelative(
  locale: string,
  value: DateInput,
  now: DateInput = Date.now(),
): string {
  const delta = toDate(value).getTime() - toDate(now).getTime();
  const span = Math.abs(delta);
  const [unit, size] = UNITS.find(([, , threshold]) => span >= threshold) ?? ["second", SECOND];
  const formatter = cached(
    "relative",
    locale,
    undefined,
    () => new Intl.RelativeTimeFormat(intlLocale(locale), { numeric: "auto" }),
  );
  // `Math.round(-0.4)` is -0, which Intl renders as "0 seconds ago"; normalise it to 0 ("now").
  return formatter.format(Math.round(delta / size) || 0, unit);
}

/** Format a number, e.g. "1,234.5" in `en`, "1.234,5" in `de`. */
export function formatNumber(
  locale: string,
  value: number | bigint,
  options?: Intl.NumberFormatOptions,
): string {
  const formatter = cached(
    "number",
    locale,
    options,
    () => new Intl.NumberFormat(intlLocale(locale), options),
  );
  return formatter.format(value);
}

/** Format a list, e.g. "Editor, Reviewer, and Admin" in `en` (default: conjunction). */
export function formatList(
  locale: string,
  items: Iterable<string>,
  options: Intl.ListFormatOptions = { type: "conjunction" },
): string {
  const formatter = cached(
    "list",
    locale,
    options,
    () => new Intl.ListFormat(intlLocale(locale), options),
  );
  return formatter.format(items);
}
