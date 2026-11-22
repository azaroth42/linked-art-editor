/**
 * Shared Tailwind class lists. Interactive targets are at least 24×24 CSS px (`min-h-6
 * min-w-6`, WCAG 2.5.8); only logical direction utilities are used (conventions §5).
 */

/** A primary action: `<button>`, or a link that starts an action. */
export const PRIMARY_BUTTON =
  "inline-flex min-h-6 min-w-6 items-center justify-center rounded bg-blue-700 px-4 py-2 font-medium text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:opacity-60";

/** A secondary action. */
export const SECONDARY_BUTTON =
  "inline-flex min-h-6 min-w-6 items-center justify-center rounded border border-slate-400 bg-white px-3 py-1 font-medium text-slate-900 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60";

/** A text link standing on its own (not inside a sentence). */
export const STANDALONE_LINK =
  "inline-flex min-h-6 items-center text-blue-700 underline underline-offset-2 hover:text-blue-900";

/** The page's `<h1>`. */
export const PAGE_HEADING = "text-2xl font-semibold text-slate-900";
