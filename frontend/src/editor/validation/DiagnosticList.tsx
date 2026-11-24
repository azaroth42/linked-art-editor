import { useCallback, useId } from "react";
import { useTranslation } from "react-i18next";

import type { ProfileSlot } from "../../api/hooks/editing.ts";
import type { Diagnostic } from "../../api/hooks/publishing.ts";
import { useFormatters } from "../../i18n/useFormatters.ts";
import { SMALL_BUTTON } from "../../widgets/styles.ts";
import { useLanguage } from "../../widgets/environment.ts";
import { localized } from "../../widgets/vocab.ts";
import { isMissingRootLabel, messageParams } from "./messages.ts";

/** A diagnostic with the slot it is about (`locate.ts`), ready to list. */
export interface LocatedDiagnostic {
  diagnostic: Diagnostic;
  slot: ProfileSlot | null;
  /** Another record of a publish bundle (a new record this one refers to); absent: this one. */
  recordId?: string | null;
}

/** A stable key for a diagnostic: what publish matches accepted warnings by, and the params. */
export function diagnosticKey(diagnostic: Diagnostic, recordId: string | null = null): string {
  return JSON.stringify([
    recordId,
    diagnostic.code,
    diagnostic.path,
    diagnostic.severity,
    diagnostic.params,
  ]);
}

/**
 * The localized message of a diagnostic: the `errors` entry of its code with its params (ICU).
 * A code without an entry falls back to the server's `message_en`, then to the code. A missing
 * root `_label` asks for a name instead: the label is derived from it (Step 1.19a).
 */
export function useDiagnosticMessage(): (diagnostic: Diagnostic) => string {
  const { t, i18n } = useTranslation(["errors", "editor"]);
  const { locale } = useFormatters();
  return useCallback(
    (diagnostic: Diagnostic) => {
      if (isMissingRootLabel(diagnostic)) {
        return t("editor:validation.missing_label");
      }
      return i18n.exists(`errors:${diagnostic.code}`)
        ? t(`errors:${diagnostic.code}`, messageParams(diagnostic.params, locale))
        : diagnostic.message_en || diagnostic.code;
    },
    [t, i18n, locale],
  );
}

const SEVERITY_STYLE: Record<Diagnostic["severity"], string> = {
  violation: "border-red-700 bg-red-50",
  warning: "border-amber-600 bg-amber-50",
  info: "border-slate-400 bg-slate-50",
};

interface DiagnosticListProps {
  items: readonly LocatedDiagnostic[];
  /** Scroll to and focus a slot; no "Go to" buttons without it. */
  onJump?: ((slotId: string) => void) | null;
}

/**
 * Diagnostics, one list item each: severity, localized message, and a button that moves to the
 * field it is about (or the pointer, when no field edits that place).
 */
export function DiagnosticList({ items, onJump = null }: DiagnosticListProps) {
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <DiagnosticItem
          key={diagnosticKey(item.diagnostic, item.recordId ?? null)}
          item={item}
          onJump={onJump}
        />
      ))}
    </ul>
  );
}

function DiagnosticItem({
  item: { diagnostic, slot, recordId = null },
  onJump,
}: {
  item: LocatedDiagnostic;
  onJump: ((slotId: string) => void) | null;
}) {
  const { t } = useTranslation("editor");
  const message = useDiagnosticMessage();
  const language = useLanguage();
  const messageId = useId();
  return (
    <li
      className={`flex flex-col gap-1 rounded border-s-4 px-3 py-2 ${SEVERITY_STYLE[diagnostic.severity]}`}
      data-severity={diagnostic.severity}
      data-code={diagnostic.code}
    >
      <span id={messageId} className="text-slate-900">
        {t("validation.item", {
          severity: t(`validation.severity.${diagnostic.severity}`),
          message: message(diagnostic),
        })}
      </span>
      {recordId !== null && (
        <span className="text-sm text-slate-700">
          {t("validation.other_record", { id: recordId })}
        </span>
      )}
      {slot !== null && onJump !== null ? (
        <div>
          <button
            type="button"
            className={SMALL_BUTTON}
            aria-describedby={messageId}
            onClick={() => onJump(slot.id)}
          >
            {t("validation.go_to", { field: localized(slot.label, language) })}
          </button>
        </div>
      ) : (
        <span className="text-sm text-slate-700">
          {diagnostic.path === ""
            ? t("validation.whole_record")
            : t("validation.location", { path: diagnostic.path })}
        </span>
      )}
    </li>
  );
}
