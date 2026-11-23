/**
 * EDTF text → the bounds of a Linked Art `TimeSpan` (Step 1.18, `EdtfWidget`).
 *
 * The syntax check is edtf.js `parse()` (pinned to 4.12.0, the version the server's
 * `lae.validation.edtf` port reproduces), never the `edtf()` constructor on the raw string,
 * which reads five or more digits as a Unix time. The parsed value then gives the earliest and
 * latest instants it can mean: `begin_of_the_begin` and `end_of_the_end`, written as
 * `xsd:dateTime` in UTC with a four-digit (or longer), signed astronomical year, so 480 BCE is
 * `-0479` (ISO 8601 and EDTF number years that way: year 0 is 1 BCE). An open or unknown end
 * (`../1900`, `1900/..`) has no bound.
 */
import edtf, { parse } from "edtf";

/** The `classified_as` of the `TimeSpan` name that holds the EDTF text (`lae.validation.rules`). */
export const EDTF_TYPE = "http://id.loc.gov/datatypes/edtf";

export type EdtfResult =
  { ok: true; begin: string | null; end: string | null } | { ok: false; error: "syntax" | "range" };

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/** `ms` as `xsd:dateTime` (`-0479-01-01T00:00:00Z`), whole seconds; `null` if out of range. */
export function formatInstant(ms: number): string | null {
  if (!Number.isFinite(ms)) {
    return null;
  }
  const date = new Date(Math.floor(ms / 1000) * 1000);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  const year = date.getUTCFullYear();
  const sign = year < 0 ? "-" : "";
  return (
    `${sign}${pad(Math.abs(year), 4)}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    `T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`
  );
}

const INSTANT =
  /^(-?\d{4,})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$/;

/**
 * An `xsd:dateTime` as a `Date` (JavaScript's own parser rejects four-digit negative years), or
 * `null` if it is not one. A value without a time zone is read as UTC.
 */
export function parseInstant(value: unknown): Date | null {
  const match = typeof value === "string" ? INSTANT.exec(value) : null;
  if (match === null) {
    return null;
  }
  const [year = 0, month = 1, day = 1, hour = 0, minute = 0, second = 0] = match
    .slice(1, 7)
    .map(Number);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, 0);
  const offset = match[7];
  if (offset !== undefined && offset !== "Z") {
    const sign = offset.startsWith("-") ? -1 : 1;
    const [hours = 0, minutes = 0] = offset.slice(1).split(":").map(Number);
    date.setTime(date.getTime() - sign * (hours * 60 + minutes) * 60_000);
  }
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Parse EDTF `text` into its outer bounds, or say why it cannot be. */
export function parseEdtf(text: string): EdtfResult {
  let parsed;
  try {
    parsed = parse(text);
  } catch {
    return { ok: false, error: "syntax" };
  }
  let value;
  try {
    value = edtf(parsed);
  } catch {
    // Well-formed, but not a value: an interval that ends before it starts.
    return { ok: false, error: "range" };
  }
  return { ok: true, begin: formatInstant(value.min), end: formatInstant(value.max) };
}
