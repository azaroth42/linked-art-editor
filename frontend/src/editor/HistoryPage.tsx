import { useCallback, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";

import { type UiProfile, useProfile } from "../api/hooks/editing.ts";
import {
  type RevisionSummary,
  useCurrentRecord,
  useRevert,
  useRevisionDiff,
  useRevisions,
} from "../api/hooks/revisions.ts";
import { PAGE_HEADING, SECONDARY_BUTTON, STANDALONE_LINK } from "../app/styles.ts";
import { useAuth } from "../app/useAuth.ts";
import { useDocumentTitle } from "../app/useDocumentTitle.ts";
import { useErrorMessage } from "../app/useErrorMessage.ts";
import { useFormatters } from "../i18n/useFormatters.ts";
import { ERROR } from "../widgets/styles.ts";
import { ConfirmDialog } from "./ConfirmDialog.tsx";
import { ChangeList } from "./diff/ChangeList.tsx";
import { editorPath, publicRecordPath, revertedState } from "./links.ts";

/** How the date of a version is shown. */
const PUBLISHED_AT: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" };

/** Which two versions are compared: `from` is the old side of the diff, `to` the new side. */
export interface Comparison {
  from: number;
  to: number;
}

/** The comparison a page opens with: the latest version against the one before it. */
export function defaultComparison(revisions: readonly RevisionSummary[]): Comparison | null {
  const last = revisions.at(-1);
  if (last === undefined) {
    return null;
  }
  return { from: revisions.at(-2)?.version ?? last.version, to: last.version };
}

/**
 * `/records/:id/history`: the Revision History Browser (Step 1.20; contract 1.14).
 *
 * - Every published version, newest first, with its date, publisher and summary, a link to its
 *   public page (`/id/{uuid}?version=N`, new tab) and, for users with `publish`, "Revert".
 * - Two radio groups choose the versions to compare, in either direction ("Swap" reverses it);
 *   the semantic diff (`GET …/revisions/{from}/diff/{to}`) is shown with the Publish dialog's
 *   change list, so a change reads the same in both places.
 * - Revert asks first and says that nothing is published, then calls `POST …/revisions/{n}/revert`
 *   and opens the editor on the new draft, which repeats it until the draft is published.
 */
export function HistoryPage() {
  const { t } = useTranslation("editor");
  const { id = "" } = useParams();
  useDocumentTitle(t("history.title"));
  const errorMessage = useErrorMessage();
  const { can } = useAuth();
  const navigate = useNavigate();
  const record = useCurrentRecord(id);
  const revisions = useRevisions(id);
  const published = record.data?.jsonld;
  const recordClass = typeof published?.type === "string" ? published.type : null;
  const recordUri = typeof published?.id === "string" ? published.id : "";
  const profile = useProfile(recordClass);
  const [chosen, setChosen] = useState<Comparison | null>(null);
  const [reverting, setReverting] = useState<number | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const list = revisions.data ?? [];
  const comparison = chosen ?? defaultComparison(list);
  const mayPublish = can("publish");

  const closeRevert = useCallback(() => {
    setReverting(null);
    opener.current?.focus();
  }, []);

  const failure = record.error ?? revisions.error;
  return (
    <div className="flex w-full flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("history.title")}</h1>
      <p className="text-slate-700">{t("history.record_id", { id })}</p>
      <Link to={editorPath(id)} className={STANDALONE_LINK}>
        {t("history.editor_link")}
      </Link>
      {failure !== null && (
        <p className={ERROR}>{errorMessage(failure) ?? t("history.load_error")}</p>
      )}
      {revisions.isPending && <p className="text-slate-700">{t("history.loading")}</p>}
      {revisions.isSuccess && list.length === 0 && (
        <p className="text-slate-700">{t("history.none")}</p>
      )}
      {list.length > 0 && comparison !== null && (
        <>
          {mayPublish && <p className="text-slate-700">{t("history.revert_help")}</p>}
          <RevisionTable
            recordId={id}
            revisions={list}
            comparison={comparison}
            onCompare={setChosen}
            mayRevert={mayPublish}
            onRevert={(version, button) => {
              opener.current = button;
              setReverting(version);
            }}
          />
          <RevisionDiff
            recordId={id}
            recordUri={recordUri}
            profile={profile.data ?? null}
            comparison={comparison}
            onSwap={() => setChosen({ from: comparison.to, to: comparison.from })}
          />
        </>
      )}
      {reverting !== null && (
        <RevertDialog
          recordId={id}
          version={reverting}
          draftEpoch={record.data?.draftEpoch ?? null}
          onCancel={closeRevert}
          onReverted={(result) => {
            void navigate(editorPath(id), {
              state: revertedState({ version: reverting, draftEpoch: result.draft_epoch }),
            });
          }}
        />
      )}
    </div>
  );
}

interface RevisionTableProps {
  recordId: string;
  revisions: readonly RevisionSummary[];
  comparison: Comparison;
  onCompare: (comparison: Comparison) => void;
  mayRevert: boolean;
  onRevert: (version: number, button: HTMLButtonElement) => void;
}

/** The versions, newest first; the two radio columns choose the comparison. */
function RevisionTable({
  recordId,
  revisions,
  comparison,
  onCompare,
  mayRevert,
  onRevert,
}: RevisionTableProps) {
  const { t } = useTranslation("editor");
  const { formatDate } = useFormatters();
  const { user } = useAuth();
  const name = useId();
  const current = revisions.at(-1)?.version;

  const publisher = (revision: RevisionSummary) => {
    if (revision.published_by === null) {
      return t("history.by_import");
    }
    return revision.published_by === user?.id ? t("history.by_you") : t("history.by_other");
  };

  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full border-collapse text-start" data-testid="revisions">
        <caption className="pb-2 text-start text-lg font-semibold text-slate-900">
          {t("history.caption", { count: revisions.length })}
        </caption>
        <thead>
          <tr className="border-b border-slate-300 text-slate-900">
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.from")}
            </th>
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.to")}
            </th>
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.version")}
            </th>
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.published")}
            </th>
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.by")}
            </th>
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.summary")}
            </th>
            <th scope="col" className="px-2 py-1 text-start">
              {t("history.actions")}
            </th>
          </tr>
        </thead>
        <tbody>
          {[...revisions].reverse().map((revision) => {
            const { version } = revision;
            return (
              <tr
                key={version}
                className="border-b border-slate-200 align-top text-slate-900"
                data-version={version}
              >
                <td className="px-2 py-1">
                  <label className="inline-flex min-h-6 min-w-6 items-center justify-center">
                    <input
                      type="radio"
                      className="size-5"
                      name={`${name}-from`}
                      aria-label={t("history.compare_from", { version })}
                      checked={comparison.from === version}
                      onChange={() => onCompare({ ...comparison, from: version })}
                    />
                  </label>
                </td>
                <td className="px-2 py-1">
                  <label className="inline-flex min-h-6 min-w-6 items-center justify-center">
                    <input
                      type="radio"
                      className="size-5"
                      name={`${name}-to`}
                      aria-label={t("history.compare_to", { version })}
                      checked={comparison.to === version}
                      onChange={() => onCompare({ ...comparison, to: version })}
                    />
                  </label>
                </td>
                <th scope="row" className="px-2 py-1 text-start font-normal">
                  {version === current
                    ? t("history.version_current", { version })
                    : t("history.version_number", { version })}
                </th>
                <td className="px-2 py-1">
                  <time dateTime={revision.created_at}>
                    {formatDate(revision.created_at, PUBLISHED_AT)}
                  </time>
                </td>
                <td className="px-2 py-1">{publisher(revision)}</td>
                <td className="px-2 py-1">
                  {revision.summary === "" ? (
                    <span className="text-slate-700">{t("history.no_summary")}</span>
                  ) : (
                    revision.summary
                  )}
                </td>
                <td className="px-2 py-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <a
                      href={publicRecordPath(recordId, version)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={STANDALONE_LINK}
                    >
                      {t("history.view", { version })}{" "}
                      <span className="sr-only">{t("history.new_tab")}</span>
                    </a>
                    {mayRevert && (
                      <button
                        type="button"
                        className={SECONDARY_BUTTON}
                        onClick={(event) => onRevert(version, event.currentTarget)}
                      >
                        {t("history.revert", { version })}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface RevisionDiffProps {
  recordId: string;
  recordUri: string;
  profile: UiProfile | null;
  comparison: Comparison;
  onSwap: () => void;
}

/** The changes that turn the `from` version into the `to` version. */
function RevisionDiff({ recordId, recordUri, profile, comparison, onSwap }: RevisionDiffProps) {
  const { t } = useTranslation("editor");
  const errorMessage = useErrorMessage();
  const { from, to } = comparison;
  const same = from === to;
  const diff = useRevisionDiff(recordId, same ? null : from, same ? null : to);
  const headingId = useId();
  const changes = diff.data?.changes;

  return (
    <section aria-labelledby={headingId} className="flex w-full flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={headingId} className="text-lg font-semibold text-slate-900">
          {t("history.diff_heading", { from, to })}
        </h2>
        <button type="button" className={SECONDARY_BUTTON} disabled={same} onClick={onSwap}>
          {t("history.swap")}
        </button>
      </div>
      {same && <p className="text-slate-700">{t("history.choose_two")}</p>}
      {diff.isFetching && <p className="text-slate-700">{t("history.diff_loading")}</p>}
      {diff.isError && (
        <p className={ERROR}>{errorMessage(diff.error) ?? t("history.diff_error")}</p>
      )}
      {changes !== undefined && !diff.isFetching && (
        <>
          <p className="text-slate-700">{t("history.change_count", { count: changes.length })}</p>
          {changes.length > 0 && (
            <ChangeList changes={changes} recordUri={recordUri} profile={profile} />
          )}
        </>
      )}
    </section>
  );
}

interface RevertDialogProps {
  recordId: string;
  version: number;
  /** The record's draft epoch as this page read it: the only draft the revert may replace. */
  draftEpoch: number | null;
  onCancel: () => void;
  onReverted: (result: { draft_epoch: number }) => void;
}

/** "Revert to version N?": the draft is replaced, nothing is published. */
function RevertDialog({ recordId, version, draftEpoch, onCancel, onReverted }: RevertDialogProps) {
  const { t } = useTranslation("editor");
  const errorMessage = useErrorMessage();
  const revert = useRevert(recordId);
  return (
    <ConfirmDialog
      title={t("revert.title", { version })}
      body={t("revert.body", { version })}
      confirmLabel={t(revert.isPending ? "revert.reverting" : "revert.confirm")}
      cancelLabel={t("revert.cancel")}
      pending={revert.isPending}
      error={revert.error === null ? null : (errorMessage(revert.error) ?? t("revert.failed"))}
      onConfirm={() => revert.mutate({ version, draftEpoch }, { onSuccess: onReverted })}
      onCancel={onCancel}
    />
  );
}
