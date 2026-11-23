import { useEffect, useId, useRef } from "react";

import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "../app/styles.ts";
import { ERROR } from "../widgets/styles.ts";

interface ConfirmDialogProps {
  title: string;
  /** What confirming does; the dialog's description. */
  body: string;
  confirmLabel: string;
  cancelLabel: string;
  /** The confirmed action is under way: both buttons and Escape are disabled. */
  pending: boolean;
  /** Why the action failed, if it did. */
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * A modal confirmation before an action that cannot be undone (Discard draft, Revert to a
 * revision). Focus starts on the safe choice, Escape cancels unless the action is under way, and
 * a failure is shown in the dialog, which stays open to try again or cancel.
 */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel,
  pending,
  error,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const bodyId = useId();
  const keep = useRef<HTMLButtonElement>(null);
  const busy = useRef(false);
  busy.current = pending;

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) {
      return undefined;
    }
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
      // Start on the safe choice, not on the destructive one.
      keep.current?.focus();
    }
    const cancel = (event: Event) => {
      event.preventDefault();
      if (!busy.current) {
        onCancel();
      }
    };
    dialog.addEventListener("cancel", cancel);
    return () => dialog.removeEventListener("cancel", cancel);
  }, [onCancel]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      className="m-auto max-w-md rounded-lg p-6 shadow-xl backdrop:bg-slate-900/50"
    >
      <h2 id={titleId} className="mb-2 text-xl font-semibold text-slate-900">
        {title}
      </h2>
      <p id={bodyId} className="mb-4 text-slate-700">
        {body}
      </p>
      {error !== null && <p className={`mb-4 ${ERROR}`}>{error}</p>}
      <div className="flex gap-2">
        <button type="button" className={PRIMARY_BUTTON} disabled={pending} onClick={onConfirm}>
          {confirmLabel}
        </button>
        <button
          ref={keep}
          type="button"
          className={SECONDARY_BUTTON}
          disabled={pending}
          onClick={onCancel}
        >
          {cancelLabel}
        </button>
      </div>
    </dialog>
  );
}
