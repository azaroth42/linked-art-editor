/** Test helpers: serve `public/locales/` to the i18n backend without a web server. */
import { vi } from "vitest";

/** Every locale file, keyed by its path relative to this module. */
export const LOCALE_FILES = import.meta.glob<Record<string, unknown>>(
  "../../public/locales/*/*.json",
  { eager: true, import: "default" },
);

/** Replace `fetch` so `/locales/<language>/<namespace>.json` is answered from `public/locales/`. */
export function stubLocaleFetch(): void {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const match = /\/locales\/([^/]+\/[^/]+\.json)$/.exec(String(input));
    const data = match === null ? undefined : LOCALE_FILES[`../../public/locales/${match[1]}`];
    return Promise.resolve(
      data === undefined ? new Response(null, { status: 404 }) : Response.json(data),
    );
  });
}
