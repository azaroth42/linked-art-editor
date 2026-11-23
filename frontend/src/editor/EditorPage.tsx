import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import { ApiError } from "../api/client.ts";
import { useEditorRecord } from "../api/hooks/records.ts";
import { PAGE_HEADING, SECONDARY_BUTTON, STANDALONE_LINK } from "../app/styles.ts";
import { useAuth } from "../app/useAuth.ts";
import { useDocumentTitle } from "../app/useDocumentTitle.ts";
import { RawInspector } from "../crdt/RawInspector.tsx";
import { CLOSE_STALE_EPOCH, useRecordDoc } from "../crdt/useRecordDoc.ts";
import { PresenceList } from "./PresenceList.tsx";
import { StaleEpochDialog } from "./StaleEpochDialog.tsx";
import { TitleField } from "./TitleField.tsx";

const INSPECTOR_ID = "raw-inspector";
/** Final close codes the page explains (ADR-0027); others get a generic message. */
const KNOWN_CLOSE_CODES = new Set([4400, 4401, 4403, 4404, 4410]);

/**
 * `/edit/:id`: the record editor. Step 1.17 connects it to the live draft (`useRecordDoc`):
 * connection state, presence, scoped undo/redo, the title, and the raw JSON-LD inspector.
 * Step 1.18 adds the profile, the Value Set bundle and the `ProfileRenderer`.
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
        <p className="text-red-800">
          {t(
            record.error instanceof ApiError && record.error.code.startsWith("LAE-")
              ? `errors:${record.error.code}`
              : "page.load_error",
          )}
        </p>
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
          </div>
          <TitleField session={live.session} disabled={!canEdit || !live.synced} />
          <button
            type="button"
            className={SECONDARY_BUTTON}
            aria-expanded={inspecting}
            aria-controls={INSPECTOR_ID}
            onClick={() => setInspecting((open) => !open)}
          >
            {t(inspecting ? "raw_inspector.hide" : "raw_inspector.show")}
          </button>
          {inspecting && <RawInspector doc={live.session.doc} id={INSPECTOR_ID} />}
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
