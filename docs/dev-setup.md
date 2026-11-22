# Development Setup: Signing In

How to authenticate against a local `lae api` (Phase 1, Step 1.7). The root `README.md` covers
installing and running the processes; this page covers the three ways in: the development user,
OpenID Connect against a real identity provider, and Personal Access Tokens.

Settings are `LAE_*` environment variables (put them in the repository-root `.env` that
`make run` loads). `LAE_SESSION_SECRET` signs the session cookie; set it to a long random value
(`openssl rand -hex 32`) so sessions survive restarts and `crdt-ws` (Step 1.8) can read them.
Without it, development and test use a random key per process; `staging` and `production`
refuse to start.

## 1. Development user (no identity provider)

```sh
LAE_DEV_USER=editor@example.org:Editor
```

Every request without a bearer token then runs as that user with exactly that role. The role is
one of `Viewer`, `Collaborator`, `Editor`, `Vocabulary Admin`, `System Admin`, `Tier 1 Agent`,
`Tier 2 Agent` (seeded by migration `0002`; run `lae migrate` first). `lae api` refuses to start
when `LAE_ENV=production` and `LAE_DEV_USER` is set, or when the value is malformed.

```sh
curl -s localhost:8000/auth/me
# {"id":"…","display_name":"editor@example.org","roles":["Editor"],"permissions":["delete","edit_draft","merge","protect","publish","unmerge"]}
```

## 2. OpenID Connect login

Any OpenID Connect provider with discovery works (institutional SSO, Keycloak, Google, GitHub
through a bridge, ORCID). The application uses the authorization code flow with PKCE (`S256`),
scopes `openid email profile`, and `client_secret_basic` at the token endpoint.

```sh
LAE_OIDC_ISSUER=http://localhost:8180/realms/lae      # discovery: <issuer>/.well-known/openid-configuration
LAE_OIDC_CLIENT_ID=lae-editor
LAE_OIDC_CLIENT_SECRET=<client secret>
LAE_SESSION_SECRET=<openssl rand -hex 32>
# unset LAE_DEV_USER, or it wins over the session
```

Register `http://localhost:8000/auth/callback` (the API's own `/auth/callback`, as seen by the
browser) as a valid redirect URI with the provider. Then open
`http://localhost:8000/auth/login?next=/` in a browser, sign in at the provider, and you are sent
back to `next` with the `lae_session` cookie set (`HttpOnly`, `SameSite=Lax`; `Secure` unless
`LAE_ENV=development`, so use HTTPS in any other environment). `GET /auth/me` shows the user;
`POST /auth/logout` ends the session.

### Keycloak, installed natively (no Docker, ADR-0017)

Keycloak needs Java 21. Either `brew install keycloak` (macOS) or unpack the distribution from
<https://www.keycloak.org/downloads> and run it in development mode on a port that is free:

```sh
KC_BOOTSTRAP_ADMIN_USERNAME=admin KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  kc.sh start-dev --http-port 8180
```

In the admin console (`http://localhost:8180/admin`):

1. Create realm `lae`.
2. Clients → Create client: type *OpenID Connect*, client id `lae-editor`; *Client
   authentication* on; *Standard flow* on (others off); *Valid redirect URIs*
   `http://localhost:8000/auth/callback`. Under *Advanced*, *Proof Key for Code Exchange Code
   Challenge Method* `S256`. Copy the secret from *Credentials* into `LAE_OIDC_CLIENT_SECRET`.
3. Users → Create user with an e-mail address, first and last name; set a password under
   *Credentials*.

## 3. Roles

A user signing in for the first time gets the `Viewer` role (no permissions). Until the admin
console exists (Phase 4 Step 4.16), grant roles in SQL:

```sql
INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id FROM users u, roles r
WHERE u.email = 'you@example.org' AND r.name = 'Editor'
ON CONFLICT DO NOTHING;
```

Role changes apply from the next request; permissions are read from the database per request.
Setting `users.is_active = false` ends the user's sessions and tokens at once.

## 4. Personal Access Tokens

Create a token while signed in (session or development user; a token cannot create tokens). The
plaintext is shown once; the database keeps only its SHA-256.

```sh
curl -s -X POST localhost:8000/api/tokens -H 'content-type: application/json' \
  -d '{"name": "etl", "scopes": ["edit_draft"], "expires_at": "2027-06-30T00:00:00Z"}'
# {"id":"…","name":"etl","scopes":["edit_draft"],…,"token":"lae_pat_…"}

curl -s localhost:8000/auth/me -H 'Authorization: Bearer lae_pat_…'
```

`scopes` must be permissions you hold; the token's effective permissions are its scopes
intersected with your current ones. `GET /api/tokens` lists your tokens (never the plaintext),
and `DELETE /api/tokens/{id}` revokes one. A bearer token always takes precedence over the
development user and the session cookie, and an invalid one is a 401 (`LAE-AUTH-003`).

## Error bodies

401 and 403 responses are structured, e.g.
`{"code": "LAE-AUTH-001", "path": "/api/records", "params": {"required": ["edit_draft"], "missing": ["edit_draft"]}}`;
the English text for each code is in `frontend/public/locales/en/errors.json`.
