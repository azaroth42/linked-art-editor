import { useTranslation } from "react-i18next";
import { Navigate, useSearchParams } from "react-router";

import { LoginOptions } from "./LoginOptions.tsx";
import { safeNext } from "./loginPaths.ts";
import { PAGE_HEADING, SECONDARY_BUTTON } from "./styles.ts";
import { useAuth } from "./useAuth.ts";
import { useDocumentTitle } from "./useDocumentTitle.ts";

/**
 * `/login?next=/path`: a signed-in user (including the dev user, `LAE_DEV_USER`) goes straight
 * to `next`; anyone else gets one sign-in button per provider the server offers (`LoginOptions`),
 * each a full-page navigation through that provider that comes back to `next`.
 */
export function LoginPage() {
  const { t } = useTranslation("common");
  const { status, retry } = useAuth();
  const [searchParams] = useSearchParams();
  const next = safeNext(searchParams.get("next"));
  useDocumentTitle(t("login.title"));

  if (status === "authenticated") {
    return <Navigate to={next} replace />;
  }
  return (
    <div className="flex max-w-prose flex-col items-start gap-4">
      <h1 className={PAGE_HEADING}>{t("login.title")}</h1>
      {status === "loading" && <p className="text-slate-700">{t("status.loading")}</p>}
      {status === "anonymous" && <LoginOptions next={next} />}
      {status === "error" && (
        <>
          <p className="text-slate-900">{t("error.unavailable")}</p>
          <button type="button" className={SECONDARY_BUTTON} onClick={retry}>
            {t("action.retry")}
          </button>
        </>
      )}
    </div>
  );
}
