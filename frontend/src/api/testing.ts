/** Test helpers: answer API requests from handlers and locale files from `public/locales/`. */
import { vi } from "vitest";

import { LOCALE_FILES } from "../i18n/testing.ts";

/** Answers one API request. */
export type Handler = (request: Request) => Response | Promise<Response>;

/** Handlers keyed by `"<METHOD> <path>"`, e.g. `"GET /auth/me"`. */
export type Routes = Record<string, Handler>;

/** A user as `GET /auth/me` returns it. */
export const EDITOR = {
  id: "0190a3b2-7c4d-7e8f-9a0b-1c2d3e4f5a6b",
  display_name: "editor@example.org",
  roles: ["Editor"],
  permissions: ["delete", "edit_draft", "merge", "protect", "publish", "unmerge"],
};

/** A structured `{code, path, params}` error response like the backend's `ApiError`. */
export function errorResponse(
  status: number,
  code: string,
  path: string,
  params: Record<string, unknown> = {},
): Response {
  return Response.json({ code, path, params }, { status });
}

/** `GET /auth/me` answered 401, as for a request without a session. */
export const anonymous: Handler = () => errorResponse(401, "LAE-AUTH-002", "/auth/me");

/**
 * Replace `fetch`: `/locales/<language>/<namespace>.json` is served from `public/locales/`, any
 * other request by the matching handler in `routes`, and anything unmatched gets a 404. Returns
 * the list of API requests made, in order.
 */
export function stubFetch(routes: Routes): Request[] {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request =
      input instanceof Request
        ? input
        : new Request(new URL(String(input), globalThis.location.origin), init);
    const url = new URL(request.url);
    const locale = /^\/locales\/([^/]+\/[^/]+\.json)$/.exec(url.pathname);
    if (locale !== null) {
      const data = LOCALE_FILES[`../../public/locales/${locale[1] ?? ""}`];
      return data === undefined ? new Response(null, { status: 404 }) : Response.json(data);
    }
    requests.push(request);
    const handler = routes[`${request.method} ${url.pathname}`];
    return handler === undefined ? new Response(null, { status: 404 }) : handler(request);
  });
  return requests;
}
