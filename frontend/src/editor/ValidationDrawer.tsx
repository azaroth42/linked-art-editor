import { useEffect, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type * as Y from "yjs";

import { ApiError } from "../api/client.ts";
import type { UiProfile } from "../api/hooks/editing.ts";
import { type Diagnostic, useRecordSchema, validateDraft } from "../api/hooks/publishing.ts";
import { SECONDARY_BUTTON } from "../app/styles.ts";
import { projectDraft } from "../crdt/derivedLabel.ts";
import { type JsonObject, ProjectionError } from "../crdt/rules.ts";
import { HELP } from "../widgets/styles.ts";
import { DiagnosticList, type LocatedDiagnostic } from "./validation/DiagnosticList.tsx";
import { slotForDiagnostic } from "./validation/locate.ts";
import { checkSchema, compileSchemaCheck, type SchemaCheck } from "./validation/schemaCheck.ts";

/** The browser's schema check runs this long after the last change (milliseconds). */
export const LIVE_CHECK_DELAY = 500;

const SEVERITY_ORDER: Record<Diagnostic["severity"], number> = {
  violation: 0,
  warning: 1,
  info: 2,
};

/** Diagnostics and the projected draft they were found in (their pointers point into it). */
interface Findings {
  items: Diagnostic[];
  document: JsonObject;
}

/** Counts of a validation result, for its announced summary. */
export function countSeverities(items: readonly Diagnostic[]) {
  return {
    violations: items.filter((item) => item.severity === "violation").length,
    warnings: items.filter((item) => item.severity === "warning").length,
    infos: items.filter((item) => item.severity === "info").length,
  };
}

/**
 * The findings to list: the browser's schema findings (always current) and the last server
 * result. While the browser check works, the server's own `LAE-SCHEMA-*` findings are left out:
 * they are the same check on an older state of the draft.
 */
export function mergeFindings(
  live: Findings | null,
  server: Findings | null,
  profile: UiProfile | null,
): LocatedDiagnostic[] {
  const located: (LocatedDiagnostic & { order: number })[] = [];
  const add = (findings: Findings, keep: (item: Diagnostic) => boolean) => {
    for (const diagnostic of findings.items.filter(keep)) {
      located.push({
        diagnostic,
        slot: profile === null ? null : slotForDiagnostic(profile, findings.document, diagnostic),
        order: located.length,
      });
    }
  };
  if (live !== null) {
    add(live, () => true);
  }
  if (server !== null) {
    add(server, (item) => live === null || !item.code.startsWith("LAE-SCHEMA-"));
  }
  const seen = new Set<string>();
  return located
    .sort(
      (a, b) =>
        SEVERITY_ORDER[a.diagnostic.severity] - SEVERITY_ORDER[b.diagnostic.severity] ||
        a.order - b.order,
    )
    .filter(({ diagnostic }) => {
      const key = JSON.stringify([diagnostic.code, diagnostic.path]);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    })
    .map(({ diagnostic, slot }) => ({ diagnostic, slot }));
}

type Live = { findings: Findings } | { error: string } | null;

function checkNow(doc: Y.Doc, check: SchemaCheck, labelLanguage: string): Live {
  try {
    const document = projectDraft(doc, labelLanguage);
    return { findings: { items: checkSchema(check, document), document } };
  } catch (error) {
    if (error instanceof ProjectionError) {
      return { error: error.code };
    }
    throw error;
  }
}

interface ValidationDrawerProps {
  recordId: string;
  recordClass: string | null;
  doc: Y.Doc;
  /**
   * The record's label language (`EditorRecord.labelLanguage`): the draft is checked with its
   * root `_label` derived in it (`projectDraft`), as the server's snapshot is.
   */
  labelLanguage: string;
  profile: UiProfile | null;
  /** The draft is loaded (synced): before that the document is empty. */
  ready: boolean;
  /** Move to a slot of the form. */
  onJump: (slotId: string) => void;
  /** Announce a validation summary in the page's live region. */
  onAnnounce: (message: string) => void;
}

/**
 * The validation drawer (Step 1.19; spec §5, "Soft Warnings … surfaced in a validation drawer").
 *
 * - While the user types, the draft is checked against its class's JSON Schema in the browser
 *   (`schemaCheck.ts`), {@link LIVE_CHECK_DELAY} after the last change of anyone's.
 * - "Validate" runs every check on the server (`POST /api/records/{id}/draft/validate`: schema,
 *   rules, SHACL, RDF) and announces the summary. The result is kept, and marked as out of date
 *   when the draft changes afterwards.
 * - Messages are the `errors` namespace entries of the codes, with `params` (ICU). A finding
 *   about a field has a button that scrolls to the field and focuses it (`locate.ts`).
 */
export function ValidationDrawer({
  recordId,
  recordClass,
  doc,
  labelLanguage,
  profile,
  ready,
  onJump,
  onAnnounce,
}: ValidationDrawerProps) {
  const { t } = useTranslation(["editor", "errors"]);
  const headingId = useId();
  const listId = useId();
  const [open, setOpen] = useState(true);
  const schema = useRecordSchema(recordClass);
  const check = useMemo<SchemaCheck | null>(() => {
    if (schema.data === undefined) {
      return null;
    }
    try {
      return compileSchemaCheck(schema.data);
    } catch {
      return null;
    }
  }, [schema.data]);
  const [live, setLive] = useState<Live>(null);
  const [server, setServer] = useState<Findings | null>(null);
  const [stale, setStale] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    if (!ready) {
      return undefined;
    }
    let timer: ReturnType<typeof setTimeout> | null = null;
    if (check !== null) {
      setLive(checkNow(doc, check, labelLanguage));
    }
    const onUpdate = () => {
      setStale(true);
      if (check === null) {
        return;
      }
      if (timer !== null) {
        clearTimeout(timer);
      }
      timer = setTimeout(() => {
        timer = null;
        setLive(checkNow(doc, check, labelLanguage));
      }, LIVE_CHECK_DELAY);
    };
    doc.on("update", onUpdate);
    return () => {
      doc.off("update", onUpdate);
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [doc, check, ready, labelLanguage]);

  const validate = async () => {
    setPending(true);
    setFailure(null);
    let document: JsonObject;
    try {
      document = projectDraft(doc, labelLanguage);
    } catch {
      document = {};
    }
    setStale(false);
    try {
      const result = await validateDraft(recordId);
      setServer({ items: result.items, document });
      onAnnounce(t("editor:validation.summary", countSeverities(result.items)));
    } catch (error) {
      setFailure(
        error instanceof ApiError && error.code.startsWith("LAE-")
          ? t(`errors:${error.code}`, error.params)
          : t("editor:validation.failed"),
      );
    } finally {
      setPending(false);
    }
  };

  const liveFindings = live !== null && "findings" in live ? live.findings : null;
  const items = mergeFindings(liveFindings, server, profile);
  const counts = countSeverities(items.map((item) => item.diagnostic));

  return (
    <aside
      aria-labelledby={headingId}
      className="flex w-full max-w-4xl flex-col gap-3 rounded border border-slate-300 p-3"
      data-testid="validation-drawer"
    >
      <div className="flex flex-wrap items-center gap-3">
        <h2 id={headingId} className="text-lg font-semibold text-slate-900">
          {t("editor:validation.title")}
        </h2>
        <button
          type="button"
          className={SECONDARY_BUTTON}
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
        >
          {t(open ? "editor:validation.hide" : "editor:validation.show", counts)}
        </button>
        <button
          type="button"
          className={SECONDARY_BUTTON}
          disabled={!ready || pending}
          onClick={() => void validate()}
        >
          {t(pending ? "editor:validation.validating" : "editor:validation.validate")}
        </button>
      </div>
      <div id={listId} hidden={!open} className="flex flex-col gap-2">
        <p className={HELP}>{t("editor:validation.help")}</p>
        {schema.isError && (
          <p className={HELP}>
            {schema.error instanceof ApiError && schema.error.code.startsWith("LAE-")
              ? t(`errors:${schema.error.code}`, schema.error.params)
              : t("editor:validation.live_unavailable")}
          </p>
        )}
        {live !== null && "error" in live && (
          <p className={HELP}>
            {t("editor:validation.live_error", { message: t(`errors:${live.error}`) })}
          </p>
        )}
        {server !== null && stale && <p className={HELP}>{t("editor:validation.stale")}</p>}
        {failure !== null && <p className="text-sm text-red-800">{failure}</p>}
        {items.length === 0 ? (
          <p className="text-slate-700">
            {t(server === null ? "editor:validation.none_live" : "editor:validation.none")}
          </p>
        ) : (
          <DiagnosticList items={items} onJump={onJump} />
        )}
      </div>
    </aside>
  );
}
