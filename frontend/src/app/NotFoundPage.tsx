import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router";

import { PAGE_HEADING, STANDALONE_LINK } from "./styles.ts";
import { useDocumentTitle } from "./useDocumentTitle.ts";

/** Every path no route matches. */
export function NotFoundPage() {
  const { t } = useTranslation("common");
  const { pathname } = useLocation();
  useDocumentTitle(t("not_found.title"));
  return (
    <div className="flex flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("not_found.title")}</h1>
      <p className="max-w-prose text-slate-700">{t("not_found.body", { path: pathname })}</p>
      <Link to="/" className={STANDALONE_LINK}>
        {t("not_found.home")}
      </Link>
    </div>
  );
}
