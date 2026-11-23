import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client.ts";
import { type DiscardResult, useDiscard } from "../api/hooks/publishing.ts";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "../app/styles.ts";
import { ERROR } from "../widgets/styles.ts";

interface DiscardButtonProps {
  recordId: string;
  disabled: boolean;
  /** Called before the request: the draft's epoch is about to end on purpose. */
  onStart: () => void;
  onDiscarded: (result: DiscardResult) => void;
  /** Called when the request failed (the draft is unchanged). */
  onFailed: () => void;
}

/**
 * "Discard draft" for Editors (Step 1.19; `POST /api/records/{id}/draft/discard`, which needs
 * `publish`). A confirmation dialog first: every unpublished change is lost for everyone editing,
 * and a record that was never published is deleted with its draft.
 */
export function DiscardButton({
  recordId,
  disabled,
  onStart,
  onDiscarded,
  onFailed,
}: DiscardButtonProps) {
  const { t } = useTranslation(["editor", "errors"]);
  const [confirming, setConfirming] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = () => {
    setConfirming(false);
    opener.current?.focus();
  };
  return (
    <>
      <button
        ref={opener}
        type="button"
        className={SECONDARY_BUTTON}
        disabled={disabled}
        onClick={() => setConfirming(true)}
      >
        {t("editor:discard.button")}
      </button>
      {confirming && (
        <ConfirmDiscard
          recordId={recordId}
          onCancel={close}
          onStart={onStart}
          onDiscarded={(result) => {
            setConfirming(false);
            onDiscarded(result);
          }}
          onFailed={onFailed}
        />
      )}
    </>
  );
}

function ConfirmDiscard({
  recordId,
  onCancel,
  onStart,
  onDiscarded,
  onFailed,
}: {
  recordId: string;
  onCancel: () => void;
  onStart: () => void;
  onDiscarded: (result: DiscardResult) => void;
  onFailed: () => void;
}) {
  const { t } = useTranslation(["editor", "errors"]);
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const bodyId = useId();
  const keep = useRef<HTMLButtonElement>(null);
  const discard = useDiscard(recordId);
  const pending = useRef(false);
  pending.current = discard.isPending;

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
      if (!pending.current) {
        onCancel();
      }
    };
    dialog.addEventListener("cancel", cancel);
    return () => dialog.removeEventListener("cancel", cancel);
  }, [onCancel]);

  const confirm = () => {
    onStart();
    discard.mutate(undefined, {
      onSuccess: (result) => onDiscarded(result),
      onError: () => onFailed(),
    });
  };

  const error = discard.error;
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      className="m-auto max-w-md rounded-lg p-6 shadow-xl backdrop:bg-slate-900/50"
    >
      <h2 id={titleId} className="mb-2 text-xl font-semibold text-slate-900">
        {t("editor:discard.title")}
      </h2>
      <p id={bodyId} className="mb-4 text-slate-700">
        {t("editor:discard.body")}
      </p>
      {error !== null && (
        <p className={`mb-4 ${ERROR}`}>
          {error instanceof ApiError && error.code.startsWith("LAE-")
            ? t(`errors:${error.code}`, error.params)
            : t("editor:discard.failed")}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          className={PRIMARY_BUTTON}
          disabled={discard.isPending}
          onClick={confirm}
        >
          {t("editor:discard.confirm")}
        </button>
        <button
          ref={keep}
          type="button"
          className={SECONDARY_BUTTON}
          disabled={discard.isPending}
          onClick={onCancel}
        >
          {t("editor:discard.cancel")}
        </button>
      </div>
    </dialog>
  );
}
