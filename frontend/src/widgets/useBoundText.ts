import { type RefObject, useLayoutEffect } from "react";

/**
 * Keep an uncontrolled text field showing `value` (a `Y.Text` read through `useYPath`) without
 * moving the caret: a change made elsewhere is applied as the one splice that turns the field's
 * text into `value`, and a selection after the splice shifts with it. (A controlled field would
 * put the caret at the end on every remote keystroke.) The field's own edits already match
 * `value` and change nothing.
 */
export function useBoundText(
  ref: RefObject<HTMLInputElement | HTMLTextAreaElement | null>,
  value: string,
): void {
  useLayoutEffect(() => {
    const field = ref.current;
    if (field === null || field.value === value) {
      return;
    }
    const old = field.value;
    let start = 0;
    while (start < old.length && start < value.length && old[start] === value[start]) {
      start += 1;
    }
    let oldEnd = old.length;
    let newEnd = value.length;
    while (oldEnd > start && newEnd > start && old[oldEnd - 1] === value[newEnd - 1]) {
      oldEnd -= 1;
      newEnd -= 1;
    }
    const focused = field.ownerDocument.activeElement === field;
    const selection = [field.selectionStart ?? 0, field.selectionEnd ?? 0] as const;
    field.value = value;
    if (focused) {
      const map = (position: number) => {
        if (position <= start) {
          return position;
        }
        return position >= oldEnd ? position + newEnd - oldEnd : newEnd;
      };
      field.setSelectionRange(map(selection[0]), map(selection[1]));
    }
  }, [ref, value]);
}
