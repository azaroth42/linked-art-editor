# Development Setup: Signing In

How to authenticate against a local `lae api` (Phase 1, Step 1.7). The root `README.md` covers
installing and running the processes; this page covers the ways in: the development user,
OpenID Connect against a real identity provider, a GitHub account, and Personal Access Tokens.
OpenID Connect and GitHub can be enabled together; the SPA's sign-in page (`/login`) then shows
one button per provider (`GET /auth/providers`).

Settings are `LAE_*` environment variables (put them in the repository-root `.env` that
`make run` loads; `.env` is git-ignored, never commit it). `LAE_SESSION_SECRET` signs the
session cookie; set it to a long random value
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

Any OpenID Connect provider with discovery works (institutional SSO, Keycloak, Google, ORCID).
GitHub is not an OpenID Connect provider; it has its own section below. The application uses the
authorization code flow with PKCE (`S256`), scopes `openid email profile`, and
`client_secret_basic` at the token endpoint.

```sh
LAE_OIDC_ISSUER=http://localhost:8180/realms/lae      # discovery: <issuer>/.well-known/openid-configuration
LAE_OIDC_CLIENT_ID=lae-editor
LAE_OIDC_CLIENT_SECRET=<client secret>
LAE_SESSION_SECRET=<openssl rand -hex 32>
# unset LAE_DEV_USER, or it wins over the session
```

Register `http://localhost:8000/auth/callback` (the API's own `/auth/callback`, as seen by the
browser) as a valid redirect URI with the provider. Then open
`http://localhost:8000/auth/login/oidc?next=/` (or `/auth/login?next=/`, which uses OpenID
Connect whenever it is configured) in a browser, sign in at the provider, and you are sent
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

## 3. GitHub login

Sign in with a GitHub account (ADR-0026). GitHub is OAuth 2.0, not OpenID Connect: the API sends
the browser to GitHub (with `state` and PKCE `S256`, scopes `read:user user:email`), exchanges
the returned code for an access token, and reads `https://api.github.com/user` and
`/user/emails` with it; the token is then discarded. Your user is
`subject = github:<your numeric GitHub id>` (renaming your GitHub login does not change it),
named after your GitHub name (or login if you have none), with your primary e-mail address if
GitHub has verified it. The first sign-in gets the `Viewer` role.

### Step 1: create a GitHub OAuth App (once)

1. On github.com: your profile picture → **Settings** → **Developer settings** → **OAuth Apps**
   → **New OAuth App**. (For an app owned by an organization, use the organization's
   **Settings** → **Developer settings** → **OAuth Apps** instead.)
2. Fill in the form:
   - **Application name**: `Laertes (local)`
   - **Homepage URL**: `http://localhost:8000`
   - **Authorization callback URL**: `http://localhost:8000/auth/callback/github`
   - **Enable Device Flow**: leave unticked.
3. Click **Register application**. On the app's page copy the **Client ID**, then click
   **Generate a new client secret** and copy the secret immediately (GitHub shows it once).

### Step 2: give the API the client id and secret (never commit them)

Put them in the repository-root `.env`, which is git-ignored (or `export` them in the shell that
runs the API). Keep the file private (`chmod 600 .env`), and check that `git status` never lists
it.

```sh
LAE_GITHUB_CLIENT_ID=<Client ID from step 1>
LAE_GITHUB_CLIENT_SECRET=<client secret from step 1>
LAE_SESSION_SECRET=<output of: openssl rand -hex 32>
# Optional, comma-separated. With neither set, any GitHub user can sign in (as Viewer).
# LAE_GITHUB_ALLOWED_USERS=<your GitHub login>
# LAE_GITHUB_ALLOWED_ORGS=<an organization whose active members may sign in>
```

Remove or comment out `LAE_DEV_USER`: it takes precedence over the session cookie, so you would
stay the development user. GitHub login is enabled only when both the id and the secret are set;
if only one is, `lae api` refuses to start and says which. The secret is a `SecretStr` and never
appears in logs.

### Step 3: run the API and sign in

```sh
make up                                                  # create the database if missing; start QLever
(cd backend && uv run --env-file ../.env lae migrate)    # first time, and after new migrations
cd backend && uv run --env-file ../.env lae api          # serves http://localhost:8000
```

Then open **`http://localhost:8000/auth/login/github?next=/auth/me`** in a browser. Use
`localhost`, not `127.0.0.1`: the callback URL is built from the address in the browser and GitHub
compares it with the one registered in step 1. GitHub asks you to authorize the app (the first
time only) and sends you back to `/auth/me`, which shows your user, signed in with the
`lae_session` cookie. `curl -s localhost:8000/auth/providers` lists the enabled providers.

The cookie belongs to the host, not the port, so the SPA on the Vite dev server
(`cd frontend && corepack pnpm dev`, `http://localhost:5173`) is signed in as well. The SPA's own
**Sign in with GitHub** button returns through `http://localhost:5173/auth/callback/github`; to
use it, add that URL as a further callback URL in the OAuth App's settings (or register a second
OAuth App for that origin).

Grant yourself a role (section 4) using your `subject`:

```sql
SELECT subject, display_name, email FROM users WHERE subject LIKE 'github:%';

INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id FROM users u, roles r
WHERE u.subject = 'github:<your numeric id>' AND r.name = 'Editor'
ON CONFLICT DO NOTHING;
```

If something goes wrong:

- GitHub says *The redirect_uri is not associated with this application*: the address in the
  browser does not match the callback URL registered in step 1 (host and port included).
- `LAE-AUTH-012`: the account is on neither allowlist. With `LAE_GITHUB_ALLOWED_ORGS`, the
  membership must be active (an unaccepted invitation does not count), and an organization that
  restricts OAuth App access must approve the app first (organization **Settings** →
  **Third-party access**).
- `LAE-AUTH-007` with `bad_verification_code`: the code expired or was already used; start again
  at `/auth/login/github`. With `incorrect_client_credentials`, check the id and secret.
- `LAE-AUTH-011`: GitHub login is not enabled (the id or secret is missing from the API's
  environment).

For a server, register a separate OAuth App whose callback URL is
`https://<your host>/auth/callback/github` and set the same variables in its environment file
(`deploy/README.md`); outside `LAE_ENV=development` the session cookie is `Secure`, so the site
must be served over HTTPS.

## 4. Roles

A user signing in for the first time gets the `Viewer` role (no permissions). Until the admin
console exists (Phase 4 Step 4.16), grant roles in SQL:

```sql
INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id FROM users u, roles r
WHERE u.email = 'you@example.org' AND r.name = 'Editor'
ON CONFLICT DO NOTHING;
```

E-mail addresses are not unique (the same address can belong to an OpenID Connect user and a
GitHub user, who are different users), and a GitHub user without a verified primary address has
none; select by `u.subject` (`github:<numeric id>`, or the OIDC `sub`) to be exact.

Role changes apply from the next request; permissions are read from the database per request.
Setting `users.is_active = false` ends the user's sessions and tokens at once.

## 5. Personal Access Tokens

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
