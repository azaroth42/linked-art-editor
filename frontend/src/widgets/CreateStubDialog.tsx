import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client.ts";
import { useCreateStub } from "../api/hooks/editing.ts";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "../app/styles.ts";
import { ERROR, FIELD, INPUT, LABEL } from "./styles.ts";

/** A record created by the dialog. */
export interface CreatedStub {
  uri: string;
  type: string;
  name: string;
}

interface CreateStubDialogProps {
  /** The record class to create. */
  recordClass: string;
  /** The name typed into the autocomplete, to start from. */
  initialName: string;
  onCreated: (stub: CreatedStub) => void;
  onCancel: () => void;
}

/**
 * "Create new …" from an entity reference: a modal that creates a stub record of `recordClass`
 * with one name (`POST /api/records`, which opens its draft) and hands it back so the caller
 * inserts the reference. Escape or Cancel closes it without creating anything.
 */
export function CreateStubDialog({
  recordClass,
  initialName,
  onCreated,
  onCancel,
}: CreateStubDialogProps) {
  const { t } = useTranslation(["editor", "errors"]);
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const nameId = useId();
  const errorId = useId();
  const [name, setName] = useState(initialName);
  const create = useCreateStub();
  const className = t(`editor:classes.${recordClass}`, { defaultValue: recordClass });

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
    }
    const cancel = (event: Event) => {
      event.preventDefault();
      onCancel();
    };
    dialog.addEventListener("cancel", cancel);
    return () => dialog.removeEventListener("cancel", cancel);
  }, [onCancel]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") {
      return;
    }
    create.mutate(
      { type: recordClass, name: trimmed },
      { onSuccess: (created) => onCreated({ uri: created.uri, type: recordClass, name: trimmed }) },
    );
  };

  const error = create.error;
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      className="m-auto w-full max-w-md rounded-lg p-6 shadow-xl backdrop:bg-slate-900/50"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <h2 id={titleId} className="text-xl font-semibold text-slate-900">
          {t("editor:widgets.create_stub.title", { class: className })}
        </h2>
        <div className={FIELD}>
          <label htmlFor={nameId} className={LABEL}>
            {t("editor:widgets.create_stub.name")}
          </label>
          <input
            id={nameId}
            type="text"
            className={INPUT}
            value={name}
            required
            aria-describedby={error === null ? undefined : errorId}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        {error !== null && (
          <p id={errorId} className={ERROR}>
            {error instanceof ApiError && error.code.startsWith("LAE-")
              ? t(`errors:${error.code}`, error.params)
              : t("editor:widgets.create_stub.failed")}
          </p>
        )}
        <div className="flex gap-2">
          <button
            type="submit"
            className={PRIMARY_BUTTON}
            disabled={create.isPending || name.trim() === ""}
          >
            {t("editor:widgets.create_stub.create")}
          </button>
          <button type="button" className={SECONDARY_BUTTON} onClick={onCancel}>
            {t("editor:widgets.create_stub.cancel")}
          </button>
        </div>
      </form>
    </dialog>
  );
}
