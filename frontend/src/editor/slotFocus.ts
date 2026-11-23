/**
 * "Go to the field" for a diagnostic (Step 1.19): the editor asks for a slot by id; the section
 * that holds it opens its details if the slot waits behind "Add details", and the slot, once it
 * is rendered, scrolls into view and takes the focus, then reports the request as done.
 */
import { createContext, useContext } from "react";

/** One request to focus a slot; `nonce` tells two requests for the same slot apart. */
export interface SlotFocusRequest {
  slotId: string;
  nonce: number;
}

export interface SlotFocus {
  request: SlotFocusRequest | null;
  /** Called by the slot once it has the focus. */
  done: (nonce: number) => void;
}

export const SlotFocusContext = createContext<SlotFocus>({ request: null, done: () => undefined });

/** The pending focus request of the enclosing editor. */
export function useSlotFocus(): SlotFocus {
  return useContext(SlotFocusContext);
}

/** Enabled form controls first, then any other focusable element. */
const CONTROLS = "input:not([disabled]), textarea:not([disabled]), select:not([disabled])";
const FOCUSABLE = "button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";

/** Scroll `frame` (a slot's container) into view and focus its first control, else itself. */
export function focusSlotFrame(frame: HTMLElement): void {
  if (typeof frame.scrollIntoView === "function") {
    frame.scrollIntoView({ block: "center" });
  }
  const target =
    frame.querySelector<HTMLElement>(CONTROLS) ?? frame.querySelector<HTMLElement>(FOCUSABLE);
  (target ?? frame).focus({ preventScroll: true });
}
