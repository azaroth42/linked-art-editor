import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router";

import { useFormatters } from "../i18n/useFormatters.ts";
import { LOGIN_PATH, loginPageUrl } from "./loginPaths.ts";
import { SECONDARY_BUTTON, STANDALONE_LINK } from "./styles.ts";
import { useAuth } from "./useAuth.ts";

const MAIN_CONTENT_ID = "main-content";

/** The layout of every SPA route: skip link, header with the user's session, and `<main>`. */
export function AppShell() {
  const { t } = useTranslation("common");
  const { formatList } = useFormatters();
  const { status, user, logout, loggingOut } = useAuth();
  const location = useLocation();

  return (
    <div className="min-h-screen bg-white text-slate-900">
      <a
        href={`#${MAIN_CONTENT_ID}`}
        className="sr-only focus:not-sr-only focus:absolute focus:start-2 focus:top-2 focus:z-10 focus:rounded focus:bg-white focus:px-3 focus:py-2"
      >
        {t("app.skip_to_main")}
      </a>
      <header className="border-b border-slate-300 bg-slate-50">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <Link
            to="/"
            className="inline-flex min-h-6 items-center text-lg font-semibold text-slate-900"
          >
            {t("app.title")}
          </Link>
          <div className="ms-auto flex flex-wrap items-center gap-x-4 gap-y-2">
            {status === "authenticated" && user !== null && (
              <>
                <p className="text-sm text-slate-700" data-testid="current-user">
                  {t("auth.signed_in_as", {
                    name: user.display_name,
                    roles: formatList(user.roles),
                  })}
                </p>
                <button
                  type="button"
                  className={SECONDARY_BUTTON}
                  onClick={() => void logout()}
                  disabled={loggingOut}
                >
                  {t("auth.sign_out")}
                </button>
              </>
            )}
            {status === "anonymous" && location.pathname !== LOGIN_PATH && (
              <Link
                to={loginPageUrl(location.pathname + location.search + location.hash)}
                className={STANDALONE_LINK}
              >
                {t("auth.sign_in")}
              </Link>
            )}
          </div>
        </div>
      </header>
      <main id={MAIN_CONTENT_ID} tabIndex={-1} className="mx-auto max-w-5xl px-4 py-6 outline-none">
        <Outlet />
      </main>
    </div>
  );
}
