import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { type LoginProvider, useLoginProviders } from "../api/hooks/auth.ts";
import { KNOWN_PROVIDERS, providerLoginUrl } from "./loginPaths.ts";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "./styles.ts";

/** The button label for `provider`: translated for known ids, else "Sign in with <name>". */
function providerLabel(t: TFunction<"common">, provider: LoginProvider): string {
  return KNOWN_PROVIDERS.has(provider.id)
    ? t(`login.provider.${provider.id}`)
    : t("login.provider_named", { name: provider.display_name });
}

/**
 * One sign-in button per provider the server offers (`GET /auth/providers`, default first).
 * Each is a link to `/auth/login/{id}`, a full-page navigation through the provider that comes
 * back to `next`.
 */
export function LoginOptions({ next }: { next: string }) {
  const { t } = useTranslation("common");
  const providers = useLoginProviders();

  if (providers.isPending) {
    return <p className="text-slate-700">{t("status.loading")}</p>;
  }
  if (providers.isError) {
    return (
      <>
        <p className="text-slate-900">{t("error.unavailable")}</p>
        <button type="button" className={SECONDARY_BUTTON} onClick={() => void providers.refetch()}>
          {t("action.retry")}
        </button>
      </>
    );
  }
  if (providers.data.length === 0) {
    return <p className="text-slate-900">{t("login.none")}</p>;
  }
  return (
    <>
      <p className="text-slate-700">{t("login.intro")}</p>
      <ul className="flex flex-col items-start gap-3">
        {providers.data.map((provider) => (
          <li key={provider.id}>
            <a href={providerLoginUrl(provider.id, next)} className={PRIMARY_BUTTON}>
              {providerLabel(t, provider)}
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}
