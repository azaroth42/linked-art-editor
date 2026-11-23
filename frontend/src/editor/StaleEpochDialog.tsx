import { useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";

import { PRIMARY_BUTTON } from "../app/styles.ts";

/**
 * Blocking dialog `editor.stale_epoch` (close code 4409): the draft was published, discarded or
 * reverted elsewhere. The only way on is "Reload", which fetches the new epoch; edits that never
 * reached the server stay in IndexedDB under the old epoch (the rebase modal is Phase 4).
 */
export function StaleEpochDialog({ onReload }: { onReload: () => void }) {
  const { t } = useTranslation("editor");
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const bodyId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) {
      return undefined;
    }
    const open = () => {
      if (!dialog.open) {
        dialog.showModal();
      }
    };
    open();
    // Blocking: Escape must not dismiss it; if the browser closes it anyway, show it again.
    const keepOpen = (event: Event) => {
      event.preventDefault();
      open();
    };
    dialog.addEventListener("cancel", keepOpen);
    dialog.addEventListener("close", open);
    return () => {
      dialog.removeEventListener("cancel", keepOpen);
      dialog.removeEventListener("close", open);
    };
  }, []);

  // showModal() moves focus to the dialog's first focusable element, the Reload button.
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      className="m-auto max-w-md rounded-lg p-6 shadow-xl backdrop:bg-slate-900/50"
    >
      <h2 id={titleId} className="mb-2 text-xl font-semibold text-slate-900">
        {t("stale_epoch.title")}
      </h2>
      <p id={bodyId} className="mb-4 text-slate-700">
        {t("stale_epoch.body")}
      </p>
      <button type="button" className={PRIMARY_BUTTON} onClick={onReload}>
        {t("stale_epoch.reload")}
      </button>
    </dialog>
  );
}
