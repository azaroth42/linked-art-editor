import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import { PAGE_HEADING, STANDALONE_LINK } from "../app/styles.ts";
import { useDocumentTitle } from "../app/useDocumentTitle.ts";

/**
 * `/edit/:id`: the record editor. Step 1.16 provides the route and page frame; Step 1.18 loads
 * the record, its UI Profile, and the Value Set bundle and renders the `ProfileRenderer`.
 */
export function EditorPage() {
  const { t } = useTranslation("editor");
  const { id = "" } = useParams();
  useDocumentTitle(t("page.title"));
  return (
    <div className="flex flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("page.title")}</h1>
      <p className="text-slate-700">{t("page.record_id", { id })}</p>
      <Link to={`/records/${encodeURIComponent(id)}/history`} className={STANDALONE_LINK}>
        {t("page.history_link")}
      </Link>
    </div>
  );
}
