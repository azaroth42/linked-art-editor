import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { useProfile, useVocabBundle } from "../api/hooks/editing.ts";
import type { DiscardResult, PublishResult } from "../api/hooks/publishing.ts";
import { useEditorRecord } from "../api/hooks/records.ts";
import { PAGE_HEADING, PRIMARY_BUTTON, SECONDARY_BUTTON, STANDALONE_LINK } from "../app/styles.ts";
import { useAuth } from "../app/useAuth.ts";
import { useDocumentTitle } from "../app/useDocumentTitle.ts";
import { useErrorMessage } from "../app/useErrorMessage.ts";
import { project } from "../crdt/project.ts";
import { RawInspector } from "../crdt/RawInspector.tsx";
import type { JsonObject } from "../crdt/rules.ts";
import { CLOSE_STALE_EPOCH, useRecordDoc } from "../crdt/useRecordDoc.ts";
import { collectReferenceLabels } from "../widgets/vocab.ts";
import { DiscardButton } from "./DiscardButton.tsx";
import { historyPath, publicRecordPath, revertedFrom } from "./links.ts";
import { PresenceList } from "./PresenceList.tsx";
import { ProfileRenderer } from "./ProfileRenderer.tsx";
import { PublishDialog } from "./PublishDialog.tsx";
import type { SlotFocus, SlotFocusRequest } from "./slotFocus.ts";
import { StaleEpochDialog } from "./StaleEpochDialog.tsx";
import { ValidationDrawer } from "./ValidationDrawer.tsx";

const INSPECTOR_ID = "raw-inspector";
/** Final close codes the page explains (ADR-0027); others get a generic message. */
const KNOWN_CLOSE_CODES = new Set([4400, 4401, 4403, 4404, 4410]);

/**
 * `/edit/:id`: the record editor. Step 1.17 connects it to the live draft (`useRecordDoc`):
 * connection state, presence, scoped undo/redo and the raw JSON-LD inspector. Step 1.18 loads
 * the UI Profile of the record's class and the Value Set bundle and renders the form with
 * `ProfileRenderer`. Step 1.19 adds the validation drawer, the Publish dialog and Discard.
 *
 * Publish and discard end the draft's epoch on purpose: `crdt-ws` then closes this page's socket
 * with 4409 like any other client's, which here is expected, not "replaced elsewhere". The
 * editor reloads the record (a new epoch, so a new document) instead of showing the stale-epoch
 * dialog. The page's one live region announces the validation summary and the publish result,
 * nothing else (spec §6.9).
 *
 * Opened by a revert (Step 1.20, navigation state `revertedFrom`), the page loads the new epoch
 * like any other and says, for as long as that draft lasts, that the draft holds the old version
 * and that nothing is published until it is.
 */
export function EditorPage() {
  const { t } = useTranslation(["editor", "errors"]);
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const errorMessage = useErrorMessage();
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
  const recordUri = typeof jsonld?.id === "string" ? jsonld.id : "";
  const profile = useProfile(recordClass);
  const bundle = useVocabBundle();
  const refLabels = useMemo(() => collectReferenceLabels(jsonld ?? null), [jsonld]);
  const [inspecting, setInspecting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [published, setPublished] = useState<PublishResult | null>(null);
  /** The epoch this page ended itself (publish, discard): its 4409 is expected. */
  const [endingEpoch, setEndingEpoch] = useState<number | null>(null);
  const [focusRequest, setFocusRequest] = useState<SlotFocusRequest | null>(null);
  const publishButton = useRef<HTMLButtonElement>(null);
  const mayEdit = can("edit_draft");
  const mayPublish = can("publish");
  const canEdit = mayEdit && !live.readOnly && live.closeCode === null;
  const usable = live.session !== null && live.synced && live.closeCode === null;
  const epoch = record.data?.draftEpoch ?? null;
  const doc = live.session?.doc ?? null;
  const reverted = revertedFrom(location.state);

  // A new epoch is loaded: whatever this page ended is over.
  useEffect(() => {
    setEndingEpoch(null);
  }, [epoch]);

  const jump = useCallback((slotId: string) => {
    setFocusRequest((previous) => ({ slotId, nonce: (previous?.nonce ?? 0) + 1 }));
  }, []);
  const focus = useMemo<SlotFocus>(
    () => ({
      request: focusRequest,
      done: (nonce) => setFocusRequest((current) => (current?.nonce === nonce ? null : current)),
    }),
    [focusRequest],
  );
  const snapshot = useCallback((): JsonObject => {
    try {
      return doc === null ? {} : project(doc);
    } catch {
      return {};
    }
  }, [doc]);

  const closePublish = useCallback(() => {
    setPublishing(false);
    publishButton.current?.focus();
  }, []);
  const onPublished = (result: PublishResult) => {
    setEndingEpoch(epoch);
    setPublishing(false);
    setPublished(result);
    setAnnouncement(t("publish.done", { version: result.version }));
    globalThis.open(publicRecordPath(id), "_blank", "noopener");
    void record.refetch();
  };
  const onDiscarded = (result: DiscardResult) => {
    if (result.deleted) {
      void navigate("/");
    } else {
      void record.refetch();
    }
  };

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
  const ending = endingEpoch !== null && endingEpoch === epoch;

  return (
    <div className="flex flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("page.title")}</h1>
      <p className="text-slate-700">{t("page.record_id", { id })}</p>
      <div aria-live="polite" aria-atomic="true" className="sr-only" data-testid="announcer">
        {announcement}
      </div>
      {reverted !== null && reverted.draftEpoch === epoch && (
        <p
          className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-slate-900"
          data-testid="reverted"
        >
          {t("revert.notice", { version: reverted.version })}
        </p>
      )}
      {published !== null && (
        <p className="text-slate-700" data-testid="published">
          {t("publish.done", { version: published.version })}{" "}
          <a
            href={publicRecordPath(id)}
            target="_blank"
            rel="noopener noreferrer"
            className={STANDALONE_LINK}
          >
            {t("publish.view", { version: published.version })}
          </a>
        </p>
      )}
      {record.isError && (
        <p className="text-red-800">{errorMessage(record.error) ?? t("page.load_error")}</p>
      )}
      {live.session !== null && doc !== null && (
        <>
          <p className="text-slate-700" data-testid="connection-status">
            {ending ? t("publish.reloading") : status}
          </p>
          {live.readOnly && <p className="text-slate-700">{t("read_only")}</p>}
          <PresenceList peers={live.peers} />
          <div className="flex flex-wrap gap-2">
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
            {mayPublish && (
              <>
                <button
                  ref={publishButton}
                  type="button"
                  className={PRIMARY_BUTTON}
                  disabled={!usable || ending}
                  onClick={() => setPublishing(true)}
                >
                  {t("publish.open")}
                </button>
                <DiscardButton
                  recordId={id}
                  disabled={!usable || ending}
                  onStart={() => setEndingEpoch(epoch)}
                  onDiscarded={onDiscarded}
                  onFailed={() => setEndingEpoch(null)}
                />
              </>
            )}
          </div>
          {inspecting && <RawInspector doc={doc} id={INSPECTOR_ID} />}
          {mayEdit && (
            <ValidationDrawer
              recordId={id}
              recordClass={recordClass}
              doc={doc}
              profile={profile.data ?? null}
              ready={live.synced}
              onJump={jump}
              onAnnounce={setAnnouncement}
            />
          )}
          {profile.isError && (
            <p className="text-red-800">{errorMessage(profile.error) ?? t("profile.load_error")}</p>
          )}
          {bundle.isError && <p className="text-red-800">{t("profile.vocab_error")}</p>}
          {profile.data !== undefined && (
            <ProfileRenderer
              profile={profile.data}
              doc={doc}
              origin={live.session.localOrigin}
              disabled={!canEdit || !live.synced}
              bundle={bundle.data ?? null}
              refLabels={refLabels}
              focus={focus}
            />
          )}
          {publishing && (
            <PublishDialog
              recordId={id}
              recordUri={recordUri}
              profile={profile.data ?? null}
              snapshot={snapshot}
              onClose={closePublish}
              onPublished={onPublished}
              onJump={(slotId) => {
                setPublishing(false);
                jump(slotId);
              }}
            />
          )}
        </>
      )}
      <Link to={historyPath(id)} className={STANDALONE_LINK}>
        {t("page.history_link")}
      </Link>
      {live.closeCode === CLOSE_STALE_EPOCH && !ending && !publishing && (
        <StaleEpochDialog onReload={() => globalThis.location.reload()} />
      )}
    </div>
  );
}
