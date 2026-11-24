import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type * as Y from "yjs";

import { projectDraft } from "./derivedLabel.ts";
import { canonicalJson, ProjectionError } from "./rules.ts";

/** The inspector re-projects the document at most this often (milliseconds). */
export const REFRESH_INTERVAL = 1000;

type View = { json: string } | { error: string };

function view(doc: Y.Doc, labelLanguage: string): View {
  try {
    return { json: canonicalJson(projectDraft(doc, labelLanguage), 2) };
  } catch (error) {
    if (error instanceof ProjectionError) {
      return { error: error.code };
    }
    throw error;
  }
}

/**
 * Read-only drawer with the draft's projected JSON-LD (the TypeScript `project()` mirror with
 * the root `_label` derived in `labelLanguage`, `projectDraft`; internal node IRIs kept, keys
 * sorted), refreshed at most once per {@link REFRESH_INTERVAL}. Mounted only while open, so a
 * closed inspector costs nothing.
 */
export function RawInspector({
  doc,
  id,
  labelLanguage,
}: {
  doc: Y.Doc;
  id: string;
  /** The record's label language (`EditorRecord.labelLanguage`). */
  labelLanguage: string;
}) {
  const { t } = useTranslation(["editor", "errors"]);
  const [current, setCurrent] = useState<View>(() => view(doc, labelLanguage));

  useEffect(() => {
    let last = Date.now();
    let timer: ReturnType<typeof setTimeout> | null = null;
    const refresh = () => {
      timer = null;
      last = Date.now();
      setCurrent(view(doc, labelLanguage));
    };
    const onUpdate = () => {
      if (timer === null) {
        timer = setTimeout(refresh, Math.max(0, REFRESH_INTERVAL - (Date.now() - last)));
      }
    };
    setCurrent(view(doc, labelLanguage));
    doc.on("update", onUpdate);
    return () => {
      doc.off("update", onUpdate);
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [doc, labelLanguage]);

  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="w-full rounded border border-slate-300 bg-slate-50 p-3"
    >
      <h2 id={`${id}-title`} className="mb-2 text-lg font-semibold text-slate-900">
        {t("editor:raw_inspector.title")}
      </h2>
      {"json" in current ? (
        <pre
          // A scrollable region must be reachable by keyboard (WCAG 2.1.1, axe
          // scrollable-region-focusable).
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          data-testid="raw-jsonld"
          className="max-h-96 overflow-auto text-sm whitespace-pre text-slate-900"
        >
          {current.json}
        </pre>
      ) : (
        <p className="text-red-800">
          {t("editor:raw_inspector.error", { message: t(`errors:${current.error}`) })}
        </p>
      )}
    </section>
  );
}
