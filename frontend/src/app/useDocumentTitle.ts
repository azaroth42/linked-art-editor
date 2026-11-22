import { useEffect } from "react";
import { useTranslation } from "react-i18next";

/**
 * Set `document.title` to "<page> – <application>" (WCAG 2.4.2 Page Titled) while the calling
 * page is shown; without a `page`, to the application name alone.
 */
export function useDocumentTitle(page?: string): void {
  const { t } = useTranslation("common");
  const title = page === undefined ? t("app.title") : t("app.page_title", { page });
  useEffect(() => {
    document.title = title;
  }, [title]);
}
