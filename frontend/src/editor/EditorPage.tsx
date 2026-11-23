import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import { ApiError } from "../api/client.ts";
import { useProfile, useVocabBundle } from "../api/hooks/editing.ts";
import { useEditorRecord } from "../api/hooks/records.ts";
import { PAGE_HEADING, SECONDARY_BUTTON, STANDALONE_LINK } from "../app/styles.ts";
import { useAuth } from "../app/useAuth.ts";
import { useDocumentTitle } from "../app/useDocumentTitle.ts";
import { RawInspector } from "../crdt/RawInspector.tsx";
import { CLOSE_STALE_EPOCH, useRecordDoc } from "../crdt/useRecordDoc.ts";
import { collectReferenceLabels } from "../widgets/vocab.ts";
import { PresenceList } from "./PresenceList.tsx";
import { ProfileRenderer } from "./ProfileRenderer.tsx";
import { StaleEpochDialog } from "./StaleEpochDialog.tsx";

const INSPECTOR_ID = "raw-inspector";
/** Final close codes the page explains (ADR-0027); others get a generic message. */
const KNOWN_CLOSE_CODES = new Set([4400, 4401, 4403, 4404, 4410]);

/** The localized message of a failed request. */
function errorKey(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.code.startsWith("LAE-")
    ? `errors:${error.code}`
    : fallback;
}

/**
 * `/edit/:id`: the record editor. Step 1.17 connects it to the live draft (`useRecordDoc`):
 * connection state, presence, scoped undo/redo and the raw JSON-LD inspector. Step 1.18 loads
 * the UI Profile of the record's class and the Value Set bundle and renders the form with
 * `ProfileRenderer`. The validation drawer and the publish button arrive with Step 1.19.
 */
export function EditorPage() {
  const { t } = useTranslation(["editor", "errors"]);
  const { id = "" } = useParams();
  const { user, can } = useAuth();
  useDocumentTitle(t("page.title"));
  const record = useEditorRecord(id);
  const live = useRecordDoc({
    recordId: id,
    epoch: record.data?.draftEpoch ?? null,
    user: user === null ? null : { id: user.id, displayName: user.display_name },
  });
  const jsonld = record.data?.jsonld;
  const recordClass = typeof jsonld?.type === "string" ? jsonld.type : null;
  const profile = useProfile(recordClass);
  const bundle = useVocabBundle();
  const refLabels = useMemo(() => collectReferenceLabels(jsonld ?? null), [jsonld]);
  const [inspecting, setInspecting] = useState(false);
  const canEdit = can("edit_draft") && !live.readOnly && live.closeCode === null;

  let status: string;
  if (live.closeCode !== null) {
    status = t(
      KNOWN_CLOSE_CODES.has(live.closeCode)
        ? `connection.closed_${String(live.closeCode)}`
        : "connection.closed",
    );
  } else if (live.connected) {
    status = t("connection.connected");
  } else {
    status = t(live.doc === null ? "connection.connecting" : "connection.offline");
  }

  return (
    <div className="flex flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("page.title")}</h1>
      <p className="text-slate-700">{t("page.record_id", { id })}</p>
      {record.isError && (
        <p className="text-red-800">{t(errorKey(record.error, "page.load_error"))}</p>
      )}
      {live.session !== null && (
        <>
          <p className="text-slate-700" data-testid="connection-status">
            {status}
          </p>
          {live.readOnly && <p className="text-slate-700">{t("read_only")}</p>}
          <PresenceList peers={live.peers} />
          <div className="flex gap-2">
            <button
              type="button"
              className={SECONDARY_BUTTON}
              onClick={live.undo}
              disabled={!canEdit || !live.canUndo}
            >
              {t("undo")}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              onClick={live.redo}
              disabled={!canEdit || !live.canRedo}
            >
              {t("redo")}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              aria-expanded={inspecting}
              aria-controls={INSPECTOR_ID}
              onClick={() => setInspecting((open) => !open)}
            >
              {t(inspecting ? "raw_inspector.hide" : "raw_inspector.show")}
            </button>
          </div>
          {inspecting && <RawInspector doc={live.session.doc} id={INSPECTOR_ID} />}
          {profile.isError && (
            <p className="text-red-800">
              {t(
                errorKey(profile.error, "profile.load_error"),
                profile.error instanceof ApiError ? profile.error.params : {},
              )}
            </p>
          )}
          {bundle.isError && <p className="text-red-800">{t("profile.vocab_error")}</p>}
          {profile.data !== undefined && (
            <ProfileRenderer
              profile={profile.data}
              doc={live.session.doc}
              origin={live.session.localOrigin}
              disabled={!canEdit || !live.synced}
              bundle={bundle.data ?? null}
              refLabels={refLabels}
            />
          )}
        </>
      )}
      <Link to={`/records/${encodeURIComponent(id)}/history`} className={STANDALONE_LINK}>
        {t("page.history_link")}
      </Link>
      {live.closeCode === CLOSE_STALE_EPOCH && (
        <StaleEpochDialog onReload={() => globalThis.location.reload()} />
      )}
    </div>
  );
}
