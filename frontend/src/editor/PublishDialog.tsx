import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client.ts";
import type { UiProfile } from "../api/hooks/editing.ts";
import {
  type AcceptedWarning,
  type Diagnostic,
  type PublishResult,
  SUMMARY_MAX_LENGTH,
  useDraftReview,
  usePublish,
} from "../api/hooks/publishing.ts";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "../app/styles.ts";
import type { JsonObject } from "../crdt/rules.ts";
import { ERROR, FIELD, HELP, LABEL, TEXTAREA } from "../widgets/styles.ts";
import { ChangeList } from "./diff/ChangeList.tsx";
import {
  DiagnosticList,
  type LocatedDiagnostic,
  useDiagnosticMessage,
} from "./validation/DiagnosticList.tsx";
import { slotForDiagnostic } from "./validation/locate.ts";

/** A diagnostic of a record of the bundle; `recordId` `null` is the record being published. */
export interface BundleDiagnostic {
  recordId: string | null;
  diagnostic: Diagnostic;
}

/** What publish matches an accepted warning by (contract 1.10 §3 item 4). */
export function warningKey({ recordId, diagnostic }: BundleDiagnostic): string {
  return JSON.stringify([recordId, diagnostic.code, diagnostic.path]);
}

function isDiagnostic(value: unknown): value is Diagnostic {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const item = value as Record<string, unknown>;
  return (
    typeof item.code === "string" &&
    typeof item.path === "string" &&
    (item.severity === "violation" || item.severity === "warning" || item.severity === "info")
  );
}

/**
 * The diagnostics in the `records` param of `LAE-PUB-002` / `LAE-PUB-003`
 * (`{record id: [diagnostics]}`), the published record's own under `null`.
 */
export function bundleDiagnostics(params: Record<string, unknown>, recordId: string) {
  const out: BundleDiagnostic[] = [];
  const records = params.records;
  if (typeof records !== "object" || records === null) {
    return out;
  }
  for (const [id, items] of Object.entries(records as Record<string, unknown>)) {
    for (const item of Array.isArray(items) ? (items as unknown[]) : []) {
      if (isDiagnostic(item)) {
        out.push({ recordId: id === recordId ? null : id, diagnostic: item });
      }
    }
  }
  return out;
}

function unique(items: BundleDiagnostic[]): BundleDiagnostic[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = warningKey(item);
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

/** The publish error codes the dialog explains with the review it already shows. */
const REVIEW_CODES = new Set(["LAE-PUB-002", "LAE-PUB-003"]);

interface PublishDialogProps {
  recordId: string;
  recordUri: string;
  profile: UiProfile | null;
  /** The projected draft now, to place the dialog's findings in the form. */
  snapshot: () => JsonObject;
  onClose: () => void;
  onPublished: (result: PublishResult) => void;
  /** Close the dialog and move to a slot of the form. */
  onJump: (slotId: string) => void;
}

/**
 * The Publish dialog (Step 1.19; spec §4.1, contract 1.10). Opening it validates the live draft
 * on the server and fetches its semantic diff (`POST …/draft/validate`, `POST …/draft/diff`).
 *
 * - The changes since the published version are listed by kind (`diff/ChangeList.tsx`).
 * - Violations are listed with a way to their fields; Publish stays disabled while any exists.
 * - Every warning has its own checkbox; Publish stays disabled until each is ticked, and exactly
 *   the ticked warnings are sent as `accepted_warnings` (a warning of a new record the draft
 *   refers to with that record's id). Warnings never shown cannot be accepted.
 * - The answers of the publish request are handled by code: `LAE-PUB-002` and `-003` add the
 *   warnings and violations they name (also those of bundled stubs) to the review;
 *   `LAE-PUB-004` (the draft changed after the check: conflict) offers to check again;
 *   `LAE-PUB-006` (the draft is based on an older version: stale) and the others explain why
 *   nothing was published.
 */
export function PublishDialog({
  recordId,
  recordUri,
  profile,
  snapshot,
  onClose,
  onPublished,
  onJump,
}: PublishDialogProps) {
  const { t } = useTranslation(["editor", "errors"]);
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const summaryId = useId();
  const review = useDraftReview(recordId, true);
  const publish = usePublish(recordId);
  const message = useDiagnosticMessage();
  const [summary, setSummary] = useState("");
  const [accepted, setAccepted] = useState<ReadonlySet<string>>(new Set());
  const [reported, setReported] = useState<BundleDiagnostic[]>([]);
  const [document, setDocument] = useState<JsonObject>({});
  // Escape must not close the dialog while a publish is under way: its answer is handled here.
  const pending = useRef(false);
  pending.current = publish.isPending;

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
      if (!pending.current) {
        onClose();
      }
    };
    dialog.addEventListener("cancel", cancel);
    return () => dialog.removeEventListener("cancel", cancel);
  }, [onClose]);

  // The findings' pointers are into the draft as it was checked.
  const reviewedAt = review.dataUpdatedAt;
  useEffect(() => {
    if (reviewedAt > 0) {
      setDocument(snapshot());
    }
  }, [reviewedAt, snapshot]);

  const checked: BundleDiagnostic[] = useMemo(
    () => (review.data?.diagnostics ?? []).map((diagnostic) => ({ recordId: null, diagnostic })),
    [review.data],
  );
  const all = unique([...checked, ...reported]);
  const violations = all.filter((item) => item.diagnostic.severity === "violation");
  const warnings = all.filter((item) => item.diagnostic.severity === "warning");
  const unaccepted = warnings.filter((item) => !accepted.has(warningKey(item)));
  const located = (items: BundleDiagnostic[]): LocatedDiagnostic[] =>
    items.map(({ recordId: owner, diagnostic }) => ({
      diagnostic,
      recordId: owner,
      slot:
        owner === null && profile !== null
          ? slotForDiagnostic(profile, document, diagnostic)
          : null,
    }));

  const recheck = () => {
    publish.reset();
    setReported([]);
    void review.refetch();
  };

  const submit = () => {
    const acceptedWarnings: AcceptedWarning[] = warnings
      .filter((item) => accepted.has(warningKey(item)))
      .map(({ recordId: owner, diagnostic }) => ({
        code: diagnostic.code,
        path: diagnostic.path,
        ...(owner === null ? {} : { record_id: owner }),
      }));
    publish.mutate(
      { summary, accepted_warnings: acceptedWarnings },
      {
        onSuccess: (result) => onPublished(result),
        onError: (error) => {
          if (error instanceof ApiError && REVIEW_CODES.has(error.code)) {
            setReported((before) => [...before, ...bundleDiagnostics(error.params, recordId)]);
          }
        },
      },
    );
  };

  const error = publish.error;
  const errorCode = error instanceof ApiError ? error.code : null;
  const canPublish =
    review.isSuccess &&
    !review.isFetching &&
    violations.length === 0 &&
    unaccepted.length === 0 &&
    !publish.isPending &&
    errorCode !== "LAE-PUB-004";

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      className="m-auto w-full max-w-3xl rounded-lg p-6 shadow-xl backdrop:bg-slate-900/50"
      data-testid="publish-dialog"
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (canPublish) {
            submit();
          }
        }}
      >
        <h2 id={titleId} className="text-xl font-semibold text-slate-900">
          {t("editor:publish.title")}
        </h2>
        {review.isFetching && <p className="text-slate-700">{t("editor:publish.loading")}</p>}
        {review.isError && (
          <p className={ERROR}>
            {review.error instanceof ApiError && review.error.code.startsWith("LAE-")
              ? t(`errors:${review.error.code}`, review.error.params)
              : t("editor:publish.load_failed")}
          </p>
        )}
        {review.data !== undefined && !review.isFetching && (
          <>
            <section aria-labelledby={`${titleId}-changes`} className="flex flex-col gap-2">
              <h3 id={`${titleId}-changes`} className="text-lg font-semibold text-slate-900">
                {t("editor:publish.changes", { count: review.data.diff.changes.length })}
              </h3>
              {review.data.diff.changes.length === 0 ? (
                <p className="text-slate-700">{t("editor:publish.no_changes")}</p>
              ) : (
                <div className="max-h-72 overflow-auto">
                  <ChangeList
                    changes={review.data.diff.changes}
                    recordUri={recordUri}
                    profile={profile}
                  />
                </div>
              )}
            </section>
            {violations.length > 0 && (
              <section aria-labelledby={`${titleId}-errors`} className="flex flex-col gap-2">
                <h3 id={`${titleId}-errors`} className="text-lg font-semibold text-slate-900">
                  {t("editor:publish.errors_heading")}
                </h3>
                <p className={HELP}>{t("editor:publish.blocked")}</p>
                <DiagnosticList items={located(violations)} onJump={onJump} />
              </section>
            )}
            {warnings.length > 0 && (
              <fieldset className="flex flex-col gap-2">
                <legend className="text-lg font-semibold text-slate-900">
                  {t("editor:publish.warnings_heading")}
                </legend>
                <p className={HELP}>{t("editor:publish.warnings_help")}</p>
                {warnings.map((item) => {
                  const key = warningKey(item);
                  return (
                    <label key={key} className="flex min-h-6 items-start gap-2 text-slate-900">
                      <input
                        type="checkbox"
                        className="mt-1 size-5 shrink-0"
                        checked={accepted.has(key)}
                        onChange={(event) => {
                          const next = new Set(accepted);
                          if (event.target.checked) {
                            next.add(key);
                          } else {
                            next.delete(key);
                          }
                          setAccepted(next);
                        }}
                      />
                      <span>
                        {item.recordId === null
                          ? t("editor:publish.accept", { message: message(item.diagnostic) })
                          : t("editor:publish.accept_other", {
                              message: message(item.diagnostic),
                              id: item.recordId,
                            })}
                      </span>
                    </label>
                  );
                })}
              </fieldset>
            )}
            <div className={FIELD}>
              <label htmlFor={summaryId} className={LABEL}>
                {t("editor:publish.summary")}
              </label>
              <textarea
                id={summaryId}
                className={TEXTAREA}
                rows={3}
                maxLength={SUMMARY_MAX_LENGTH}
                value={summary}
                onChange={(event) => setSummary(event.target.value)}
              />
            </div>
          </>
        )}
        {error !== null && (
          <div className="flex flex-col items-start gap-2" data-testid="publish-error">
            <p className={ERROR}>
              {errorCode !== null && errorCode.startsWith("LAE-")
                ? t(`errors:${errorCode}`, (error as ApiError).params)
                : t("editor:publish.failed")}
            </p>
            {errorCode === "LAE-PUB-004" && (
              <button type="button" className={SECONDARY_BUTTON} onClick={recheck}>
                {t("editor:publish.check_again")}
              </button>
            )}
          </div>
        )}
        {review.isSuccess && violations.length === 0 && unaccepted.length > 0 && (
          <p className={HELP}>{t("editor:publish.confirm_warnings")}</p>
        )}
        <div className="flex gap-2">
          <button type="submit" className={PRIMARY_BUTTON} disabled={!canPublish}>
            {t(publish.isPending ? "editor:publish.publishing" : "editor:publish.submit")}
          </button>
          <button
            type="button"
            className={SECONDARY_BUTTON}
            disabled={publish.isPending}
            onClick={onClose}
          >
            {t("editor:publish.cancel")}
          </button>
        </div>
      </form>
    </dialog>
  );
}
