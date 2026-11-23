/**
 * Tailwind class lists shared by the widgets. Every control is at least 24×24 CSS px
 * (`min-h-6 min-w-6`, WCAG 2.5.8); only logical direction utilities are used (conventions §5).
 */

/** A slot: a fieldset whose legend is the slot label. */
export const SLOT = "flex w-full min-w-0 flex-col gap-2 rounded border border-slate-300 p-3";

/** The legend of a slot. */
export const SLOT_LEGEND = "px-1 font-semibold text-slate-900";

/** Help text under a legend or label. */
export const HELP = "text-sm text-slate-700";

/** A label above a control. */
export const LABEL = "text-sm font-medium text-slate-900";

/** A one-line text or number input. */
export const INPUT =
  "min-h-6 w-full rounded border border-slate-500 px-2 py-1 text-slate-900 disabled:bg-slate-100";

/** A multi-line text input. */
export const TEXTAREA =
  "min-h-16 w-full rounded border border-slate-500 px-2 py-1 text-slate-900 disabled:bg-slate-100";

/** A select. */
export const SELECT =
  "min-h-6 rounded border border-slate-500 bg-white px-2 py-1 text-slate-900 disabled:bg-slate-100";

/** One entry of a multi-valued slot. */
export const ENTRY = "flex flex-col gap-2 rounded border border-slate-200 bg-slate-50 p-2";

/** A row of controls that wraps. */
export const ROW = "flex flex-wrap items-end gap-3";

/** A labelled control in a {@link ROW}. */
export const FIELD = "flex flex-col gap-1";

/** A small secondary button (add, remove). */
export const SMALL_BUTTON =
  "inline-flex min-h-6 min-w-6 items-center justify-center rounded border border-slate-500 bg-white px-2 py-1 text-sm font-medium text-slate-900 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60";

/** A toggle pill; `aria-pressed` styles the pressed state. */
export const PILL =
  "inline-flex min-h-6 min-w-6 items-center rounded-full border border-slate-500 bg-white px-3 py-1 text-sm text-slate-900 hover:bg-slate-100 aria-pressed:border-blue-800 aria-pressed:bg-blue-700 aria-pressed:text-white disabled:cursor-not-allowed disabled:opacity-60";

/** An inline error under a control. */
export const ERROR = "text-sm text-red-800";
