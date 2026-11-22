# ADR-0020: Session cookie, token rules, and the role seed

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Claude Opus 5.5 (Phase 1 Step 1.7, conventions §8.2: the guide and spec are silent on these points) · **Recorded by:** Claude

## Context
Step 1.7 asks for OIDC login with "a signed session cookie", PATs, `ROLE_PERMISSIONS` for four roles, and a dev-user bypass. It does not say how the session is stored or revoked, which permissions the other three roles of spec §10.4 hold, which roles a new user gets, how a bearer token interacts with the dev user, whether a token may mint tokens, or what a 401/403 body looks like.

## Decision
1. **Session.** Starlette `SessionMiddleware` cookie `lae_session`, signed with `LAE_SESSION_SECRET` (itsdangerous; not encrypted, it holds only the user id, the issue time `iat`, and Authlib's login state), `HttpOnly`, `SameSite=Lax`, `Secure` unless `LAE_ENV=development`, 12-hour idle lifetime. There is no server-side session table: every request reloads the user, so `users.is_active = false` ends sessions at once, and `iat` lets Phase 4's `lae.auth.sessions.revoke_all` reject sessions issued before a cut-off without reshaping the cookie. The secret is required in `staging` and `production`.
2. **Roles.** Migration `0002` seeds all seven roles of spec §10.4 now: Viewer `{}`, Collaborator `{edit_draft}`, Editor `{edit_draft, publish, delete, merge, unmerge, protect}`, System Admin (all fifteen), Tier 1 Agent `{edit_draft, propose_merge}`, Tier 2 Agent `{review_proposal, approve_merge_scoped}` (spec §8.2), and Vocabulary Admin `{manage_vocabularies}` (spec §7 names the role; roles are additive, so a vocabulary admin who also edits records holds Editor as well). A user signing in with no role gets Viewer.
3. **Tokens.** SHA-256 stored, looked up by digest and compared with `hmac.compare_digest`; effective permissions are `scopes ∩` the user's *current* role permissions; scopes must be held at creation (403 `LAE-AUTH-004`); tokens are created only from a session or the dev user, never with a token (403 `LAE-AUTH-005`), so a leaked token cannot mint a longer-lived one.
4. **Precedence.** A bearer token wins over `LAE_DEV_USER`, which wins over the session cookie; an invalid bearer token is a 401 and never falls back.
5. **Error bodies.** `lae.api.errors.ApiError` answers with `{code, path, params}`, `path` being the request path; 401s carry `WWW-Authenticate: Bearer`.

## Consequences
Signing in needs no extra table, and suspension is immediate. Sessions cannot be listed or revoked one by one until Phase 4 adds a revocation cut-off. Changing a role's permissions needs a new migration (or the Phase 4 role editor), and every later phase's permission checks find their roles already seeded. Scripts that want a new token must use a signed-in session or the CLI that Phase 3 adds for service accounts.

*Amended 2026-11-22 (orchestrator, per spec §8.2):* System Admin no longer holds editorial permissions; migration 0003 restricts it to the administrative permissions. See `docs/questions.md` Step 1.7.
