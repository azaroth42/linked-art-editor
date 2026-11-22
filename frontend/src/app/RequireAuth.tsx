import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation } from "react-router";

import { loginPageUrl } from "./loginPaths.ts";
import { SECONDARY_BUTTON } from "./styles.ts";
import { useAuth } from "./useAuth.ts";

/**
 * Render `children` for a signed-in user. Anonymous visitors are sent to the login page, which
 * returns here afterwards; while `/auth/me` loads a placeholder is shown, and if it fails (the
 * API is down or unreachable), a message with a retry button.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { t } = useTranslation("common");
  const { status, retry } = useAuth();
  const location = useLocation();

  switch (status) {
    case "authenticated":
      return children;
    case "anonymous":
      return (
        <Navigate to={loginPageUrl(location.pathname + location.search + location.hash)} replace />
      );
    case "loading":
      return <p className="text-slate-700">{t("status.loading")}</p>;
    case "error":
      return (
        <div className="flex flex-col items-start gap-3">
          <p className="text-slate-900">{t("error.unavailable")}</p>
          <button type="button" className={SECONDARY_BUTTON} onClick={retry}>
            {t("action.retry")}
          </button>
        </div>
      );
  }
}
