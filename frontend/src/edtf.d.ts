/**
 * The part of edtf.js 4.12.0 (`edtf` on npm, which ships no types) the editor uses.
 * `parse` answers the syntax question exactly as the server's `lae.validation.edtf` does;
 * `edtf(parsed)` builds the value whose `min` and `max` are the bounds in epoch milliseconds.
 */
declare module "edtf" {
  /** What `parse` returns: the value's kind and its parsed parts. */
  export interface ParseResult {
    type: string;
    level: number;
    [key: string]: unknown;
  }

  /** An EDTF value: `min` and `max` are epoch milliseconds (`±Infinity` for an open end). */
  export interface ExtDateTime {
    readonly type: string;
    readonly min: number;
    readonly max: number;
    edtf: string;
  }

  /** Parse `text` (level 2 by default); throws on a syntax error. */
  export function parse(text: string, options?: { level?: number; types?: string[] }): ParseResult;

  /** Build the value of a parse result; throws if, for example, an interval ends before it starts. */
  export default function edtf(parsed: ParseResult): ExtDateTime;
}
