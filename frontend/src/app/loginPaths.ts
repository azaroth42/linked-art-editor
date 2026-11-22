/** Where sign-in starts and where it returns to (mirrors `lae.auth.oidc.safe_next`). */

/** The SPA's sign-in page. */
export const LOGIN_PATH = "/login";

/** The API endpoint that redirects to the identity provider (`GET /auth/login?next=`). */
export const OIDC_LOGIN_PATH = "/auth/login";

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

/** The API URL that starts the OpenID Connect login and returns to `next` afterwards. */
export function oidcLoginUrl(next: string): string {
  return `${OIDC_LOGIN_PATH}?${new URLSearchParams({ next: safeNext(next) }).toString()}`;
}
