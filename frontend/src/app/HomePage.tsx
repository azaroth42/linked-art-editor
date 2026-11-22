import { useTranslation } from "react-i18next";

import { PAGE_HEADING } from "./styles.ts";
import { useDocumentTitle } from "./useDocumentTitle.ts";

/** `/`: the signed-in landing page. */
export function HomePage() {
  const { t } = useTranslation("common");
  useDocumentTitle();
  return (
    <div className="flex flex-col gap-4">
      <h1 className={PAGE_HEADING}>{t("app.title")}</h1>
      <p className="max-w-prose text-slate-700">{t("home.intro")}</p>
    </div>
  );
}
