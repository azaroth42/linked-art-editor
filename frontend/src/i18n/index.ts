/**
 * i18n bootstrap: react-i18next with ICU MessageFormat (spec §6.7, decision D10).
 *
 * Strings live in `public/locales/<language>/<namespace>.json` and are fetched one namespace at a
 * time on first use, so each route loads only what it needs. The development-only `pseudo` locale
 * reuses the `en` strings and passes them through {@link pseudoLocalize}.
 */
import i18next, { type BackendModule, type i18n as I18n, type ResourceKey } from "i18next";
import ICU from "i18next-icu";
import { initReactI18next } from "react-i18next";

import { PSEUDO_LANGUAGE, PSEUDO_POST_PROCESSOR, pseudoPostProcessor } from "./pseudo.ts";

export { PSEUDO_LANGUAGE, pseudoLocalize } from "./pseudo.ts";

export const NAMESPACES = [
  "common",
  "editor",
  "reader",
  "graph",
  "vocab_admin",
  "errors",
  "admin",
] as const;
export type Namespace = (typeof NAMESPACES)[number];

export const DEFAULT_NAMESPACE: Namespace = "common";
export const DEFAULT_LANGUAGE = "en";

/** Languages the UI can be switched to. `pseudo` exists only in development and test builds. */
export const SUPPORTED_LANGUAGES: readonly string[] = import.meta.env.PROD
  ? [DEFAULT_LANGUAGE]
  : [DEFAULT_LANGUAGE, PSEUDO_LANGUAGE];

/** URL of one namespace file under `public/`; the pseudo locale is derived from `en`. */
export function localeUrl(language: string, namespace: string): string {
  const source = language === PSEUDO_LANGUAGE ? DEFAULT_LANGUAGE : language;
  return `${import.meta.env.BASE_URL}locales/${source}/${namespace}.json`;
}

/** The `?lng=` query parameter when it names a supported language, otherwise {@link DEFAULT_LANGUAGE}. */
export function initialLanguage(search: string = window.location.search): string {
  const requested = new URLSearchParams(search).get("lng");
  return requested !== null && SUPPORTED_LANGUAGES.includes(requested)
    ? requested
    : DEFAULT_LANGUAGE;
}

const localeBackend: BackendModule = {
  type: "backend",
  callbackApi: true,
  init() {},
  read(language, namespace, callback) {
    fetch(localeUrl(language, namespace))
      .then((response) => {
        if (!response.ok) {
          throw new Error(`${String(response.status)} ${localeUrl(language, namespace)}`);
        }
        return response.json() as Promise<ResourceKey>;
      })
      .then(
        (data) => callback(null, data),
        (error: unknown) => callback(error instanceof Error ? error : String(error), false),
      );
  },
};

/**
 * Create an i18next instance and start loading the `common` namespace. Render under an
 * `I18nextProvider` and a `Suspense` boundary; components suspend until their namespaces load.
 */
export function createI18n(language: string = initialLanguage()): I18n {
  const instance = i18next.createInstance();
  void instance
    .use(localeBackend)
    .use(ICU)
    .use(pseudoPostProcessor)
    .use(initReactI18next)
    .init({
      lng: language,
      fallbackLng: DEFAULT_LANGUAGE,
      supportedLngs: [...SUPPORTED_LANGUAGES],
      ns: [DEFAULT_NAMESPACE],
      defaultNS: DEFAULT_NAMESPACE,
      postProcess: [PSEUDO_POST_PROCESSOR],
      interpolation: { escapeValue: false },
    });
  return instance;
}
