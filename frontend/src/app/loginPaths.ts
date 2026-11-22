/** Where sign-in starts and where it returns to (mirrors `lae.auth.oidc.safe_next`). */

/** The SPA's sign-in page. */
export const LOGIN_PATH = "/login";

/** The API endpoints that redirect to an identity provider (`GET /auth/login/{provider}`). */
export const AUTH_LOGIN_PATH = "/auth/login";

/** Provider ids whose button label is translated (`login.provider.<id>` in `common`). */
export const KNOWN_PROVIDERS: ReadonlySet<string> = new Set(["oidc", "github"]);

/**
 * `value` if it is a same-origin absolute path other than the login page, else `/`. Anything
 * else (`//host`, `https://…`, backslashes, control characters) could redirect off-site.
 */
export function safeNext(value: string | null | undefined): string {
  if (
    typeof value !== "string" ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("\\") ||
    /\p{Cc}/u.test(value)
  ) {
    return "/";
  }
  const path = value.split(/[?#]/, 1)[0];
  return path === LOGIN_PATH ? "/" : value;
}

/** The SPA login page URL that returns to `next` afterwards. */
export function loginPageUrl(next: string): string {
  const target = safeNext(next);
  return target === "/"
    ? LOGIN_PATH
    : `${LOGIN_PATH}?${new URLSearchParams({ next: target }).toString()}`;
}

/** The API URL that starts signing in with `provider` and returns to `next` afterwards. */
export function providerLoginUrl(provider: string, next: string): string {
  const query = new URLSearchParams({ next: safeNext(next) }).toString();
  return `${AUTH_LOGIN_PATH}/${encodeURIComponent(provider)}?${query}`;
}
