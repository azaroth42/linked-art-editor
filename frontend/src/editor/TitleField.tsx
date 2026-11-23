import { useId } from "react";
import { useTranslation } from "react-i18next";

import { addNode, setScalar, type YMap } from "../crdt/paths.ts";
import type { RecordSession } from "../crdt/useRecordDoc.ts";
import { useYPath } from "../crdt/useYPath.ts";

/**
 * The record's first `Name` (`identified_by[Name]`), edited as plain text: the Step 1.17 editing
 * surface that shows co-editing end to end. Step 1.18 replaces it with the profile's `title`
 * slot (`LangTextWidget`). Every keystroke is one `setScalar`, which splices the `Y.Text` with
 * the minimal code-point change (R23), so concurrent edits elsewhere in the title survive.
 */
export function TitleField({ session, disabled }: { session: RecordSession; disabled: boolean }) {
  const { t } = useTranslation("editor");
  const id = useId();
  const names = useYPath(session.doc, "identified_by[Name]");
  const entry = (names.values[0] as YMap | undefined) ?? null;
  const content = useYPath(session.doc, "content", { base: entry });
  const value = entry === null ? "" : String(content.json[0] ?? "");

  const onChange = (next: string) => {
    if (entry === null) {
      addNode(
        session.doc,
        "identified_by[Name]",
        { content: next },
        { origin: session.localOrigin },
      );
    } else {
      setScalar(session.doc, "content", next, { base: entry, origin: session.localOrigin });
    }
  };

  return (
    <div className="flex w-full max-w-xl flex-col gap-1">
      <label htmlFor={id} className="font-medium text-slate-900">
        {t("title_field.label")}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-6 rounded border border-slate-400 px-2 py-1 text-slate-900 disabled:bg-slate-100"
      />
    </div>
  );
}
