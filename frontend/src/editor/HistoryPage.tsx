import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import { PAGE_HEADING, STANDALONE_LINK } from "../app/styles.ts";
import { useDocumentTitle } from "../app/useDocumentTitle.ts";

/**
 * `/records/:id/history`: the Revision History Browser. Step 1.16 provides the route and page
 * frame; Step 1.20 adds the revision list, the diff, and revert.
 */
export function HistoryPage() {
  const { t } = useTranslation("editor");
  const { id = "" } = useParams();
  useDocumentTitle(t("history.title"));
  return (
    <div className="flex flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("history.title")}</h1>
      <p className="text-slate-700">{t("history.record_id", { id })}</p>
      <Link to={`/edit/${encodeURIComponent(id)}`} className={STANDALONE_LINK}>
        {t("history.editor_link")}
      </Link>
    </div>
  );
}
