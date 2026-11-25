import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { type DiscardResult, useDiscard } from "../api/hooks/publishing.ts";
import { SECONDARY_BUTTON } from "../app/styles.ts";
import { useErrorMessage } from "../app/useErrorMessage.ts";
import { ConfirmDialog } from "./ConfirmDialog.tsx";

interface DiscardButtonProps {
  recordId: string;
  /** Epoch of the draft on screen: the only draft the request may discard (`null`: unknown). */
  draftEpoch: number | null;
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
  draftEpoch,
  disabled,
  onStart,
  onDiscarded,
  onFailed,
}: DiscardButtonProps) {
  // The dialog's error messages load with the button, so opening it never suspends.
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
          draftEpoch={draftEpoch}
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
  draftEpoch,
  onCancel,
  onStart,
  onDiscarded,
  onFailed,
}: {
  recordId: string;
  draftEpoch: number | null;
  onCancel: () => void;
  onStart: () => void;
  onDiscarded: (result: DiscardResult) => void;
  onFailed: () => void;
}) {
  const { t } = useTranslation("editor");
  const errorMessage = useErrorMessage();
  const discard = useDiscard(recordId);
  const confirm = () => {
    onStart();
    discard.mutate(draftEpoch, {
      onSuccess: (result) => onDiscarded(result),
      onError: () => onFailed(),
    });
  };
  return (
    <ConfirmDialog
      title={t("discard.title")}
      body={t("discard.body")}
      confirmLabel={t("discard.confirm")}
      cancelLabel={t("discard.cancel")}
      pending={discard.isPending}
      error={discard.error === null ? null : (errorMessage(discard.error) ?? t("discard.failed"))}
      onConfirm={confirm}
      onCancel={onCancel}
    />
  );
}
