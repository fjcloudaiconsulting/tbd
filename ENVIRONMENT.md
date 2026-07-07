# Environment Variables

Source of truth for every environment variable that The Better Decision (pfv)
reads. If a variable is not listed here, it is not officially supported and may
be removed or renamed without notice.

## Audience

Anyone deploying, operating, or running pfv locally:

- Local developers (`docker compose` + `./pfv`)
- CI maintainers (GitHub Actions secrets, smoke tests)
- Production operators (DigitalOcean App Platform, `doctl apps update`)

## Table of contents

1. [Quick start](#quick-start)
2. [Variable reference (by component)](#variable-reference-by-component)
3. [Feature flags and runtime modes](#feature-flags-and-runtime-modes)
4. [Secrets vs non-secrets](#secrets-vs-non-secrets)
5. [Deployment paths](#deployment-paths)
6. [Common failure modes](#common-failure-modes)
7. [Spec-sync hazards (DigitalOcean App Platform)](#spec-sync-hazards-digitalocean-app-platform)
8. [Related files](#related-files)

---

## Quick start

### Local development

Minimum to bring up the stack with `docker compose up`:

```bash
cp .env.example .env
# Open .env and set JWT_SECRET_KEY to a real 32+ char secret:
#   python -c "import secrets; print(secrets.token_urlsafe(64))"
./pfv start
```

The rest of `.env.example` is pre-populated with safe local-dev defaults.

### Production (DigitalOcean App Platform)

Production env vars live in `.do/app.yaml` and are pushed with:

```bash
doctl apps update <APP_ID> --spec .do/app.yaml
```

The current production app ID is `3bcf70e8-2bae-4918-8297-ce430c79735e`.

`SECRET`-scoped values are encrypted as `EV[...]` blobs and safe to commit.
See [Spec-sync hazards](#spec-sync-hazards-digitalocean-app-platform) below.

---

## Variable reference (by component)

Columns: name, required, default, where to set (Local / CI / Prod),
sensitive, purpose, failure mode if missing.

### Backend (FastAPI)

Loaded by `backend/app/config.py` via pydantic-settings (`.env` file +
process env). Every value has an env override; the name in the table is
the env-var name (uppercased).

| Variable | Required | Default | Local | CI | Prod | Sensitive | Purpose | Failure mode if missing |
|---|---|---|---|---|---|---|---|---|
| `APP_NAME` | no | `"The Better Decision"` | `.env` | conftest | `.do/app.yaml` | no | Display name in emails and Swagger title. | Default used. |
| `APP_ENV` | yes | `development` | `.env` | conftest | `.do/app.yaml` (`production`) | no | Selects dev vs prod code paths (CORS, cookies, MFA fallback, lifespan migrations). | Defaults to `development`. Prod-only code paths are skipped. |
| `LOG_LEVEL` | no | `INFO` | `.env` | conftest | `.do/app.yaml` | no | structlog level filter. | Defaults to `INFO`. |
| `DATABASE_URL` | yes | `mysql+aiomysql://pfv2:pfv2_secret@mysql:3306/pfv2` | `.env` | conftest sets a placeholder | `.do/app.yaml` (SECRET, also bound to migrate job) | yes (prod) | Async SQLAlchemy DSN. Alembic and the app share it. | Backend cannot reach MySQL; lifespan and Alembic fail. |
| `DB_POOL_SIZE` | no | `5` | `.env` | unset | `.do/app.yaml` (optional override) | no | SQLAlchemy pool size per replica. See K8S-3 (PR #251). | Default 5 used. |
| `DB_MAX_OVERFLOW` | no | `10` | `.env` | unset | `.do/app.yaml` (optional override) | no | SQLAlchemy max overflow per replica. See K8S-3 (PR #251). | Default 10 used. |
| `JWT_SECRET_KEY` | yes | none (placeholder rejected) | `.env` | conftest sets a long fixture value | `.do/app.yaml` SECRET, also bound to migrate job | yes | HS256 key for access / refresh / reset / step-up / invite / verify-email tokens. Also keyed by recovery-code HMAC and MFA Fernet derivation. | Backend refuses to boot (`field_validator` rejects placeholder; min length 32). |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | no | `15` | `.env` | unset | inherit default | no | Access-token lifetime. | Default 15. |
| `SESSION_LIFETIME_DAYS` | no | `30` | `.env` | unset | inherit default | no | Session TTL in days — drives the refresh cookie `Max-Age`, the refresh JWT `exp` claim, the Redis primary-key TTL, AND the absolute-lifetime check, all in lockstep. Per-org override via `OrgSetting(key="session_lifetime_days")` (set from the Security settings page; 1-365, validated). Validator enforces `1 <= v <= 365`; out-of-range values refuse to boot. Unified into a single TTL by the 2026-05-18 session-stability refactor — previously split between `REFRESH_IDLE_TTL_DAYS` (idle) and `SESSION_LIFETIME_DAYS` (absolute), which left the org setting decorative for any value above the idle TTL. | Default 30. |
| `COOKIE_SECURE` | yes for prod | `true` | `.env` set to `false` for HTTP | `false` via conftest | `.do/app.yaml` (`true`) | no | Marks refresh / step-up / sso-state cookies `Secure`. | If `true` on HTTP, browsers drop the cookie and login loops. |
| `AUTH_DEBUG_LOGGING` | no | `false` | `.env` | `true` via conftest autouse | unset (defaults to `false`) | no | Gates the `auth.refresh.rejected` structlog events emitted at every terminal-401 raise site in `/auth/refresh`. Default `false` keeps INFO logs quiet under normal operation. Flip to `true` during incident triage to capture the `reason` enum, then back to `false` once the diagnosis is in hand. The 401 itself is NOT gated — only the diagnostic emission. | Operator can't distinguish the eleven 401 paths in logs until the flag is on. |
| `REDIS_URL` | yes for prod | `""` | `.env` (`redis://redis:6379/0`) | unset (in-memory fallback) | `.do/app.yaml` SECRET | yes (prod) | Backing store for slowapi rate limiting (cross-replica) and MFA email-fallback codes. | Rate limiter falls back to per-replica in-memory; MFA email-fallback returns 503 in production. |
| `MAILGUN_API_KEY` | no | `""` | `.env` (empty for console logging) | unset | `.do/app.yaml` SECRET | yes | Mailgun API key. When empty, `send_email` logs subject/recipient only. | Email sends are silently skipped (dev-mode logger). |
| `MAILGUN_DOMAIN` | with `MAILGUN_API_KEY` | `""` | `.env` | unset | `.do/app.yaml` (`m.thebetterdecision.com`) | no | Mailgun sending domain. | Mailgun call URL is malformed; send fails. |
| `MAILGUN_REGION` | no | `""` | `.env` (empty for US) | unset | `.do/app.yaml` (`eu`) | no | `eu` selects `api.eu.mailgun.net`. Empty selects the US endpoint. | Wrong region returns Mailgun 401 / 404. |
| `EMAIL_FROM` | no | `"The Better Decision <noreply@thebetterdecision.com>"` | `.env` | unset | `.do/app.yaml` | no | RFC-5322 `From:` header for outbound mail. | Default sender used. |
| `APP_URL` | yes | `http://localhost` | `.env` | unset | `.do/app.yaml` (`https://app.thebetterdecision.com`) | no | Base URL embedded in password-reset, email-verify, MFA, invite, and Google SSO callback links. | Email links point at localhost. |
| `MFA_ENCRYPTION_KEY` | yes if MFA used | `""` | `.env` (Fernet key) | unset | `.do/app.yaml` SECRET | yes | Fernet key for `users.mfa_secret_encrypted` at rest. | TOTP enroll / verify returns 500. |
| `AI_CREDENTIAL_ENCRYPTION_KEY` | yes when AI providers used | `""` | `.env` (Fernet key) | unset | `.do/app.yaml` SECRET | yes | Fernet key for `org_ai_credentials.encrypted_api_key` / `encrypted_bearer_token`. MUST differ from `MFA_ENCRYPTION_KEY`; lifespan KEK guard fatal-logs `config.ai_credential_key_reuses_mfa_key` and refuses to boot on collision (except `APP_ENV=test`). | All `/api/v1/settings/ai-providers` writes return 500. |
| `AI_CREDENTIAL_ENCRYPTION_KEY_PREV` | no | `""` | unset | unset | `.do/app.yaml` SECRET during rotation | yes | Previous-rotation Fernet key. Decrypt falls back to it when current key fails. Clear after re-encrypting all rows. | Existing rows can't be decrypted post-rotation until cleared. |
| `AI_NATIVE_ENABLED` | no | `false` | `.env` `false` | `.env` `false` | `.do/app.yaml` `false` | no | Master gate for the native (server-hosted) provider option. Flipped on with the native adapter + consent UI in PR4. | Native option hidden in the UI. |
| `AI_NATIVE_CURRENT_CONSENT_VERSION` | no | `"ai-tos-2026-05-22"` | `.env` (same default) | `.env` (same default) | `.do/app.yaml` (same default) | no | Pinned ToS version for the native-provider consent flow. POSTs to `/api/v1/settings/ai-providers/consent` must carry this exact `consent_version` string — any mismatch (older OR newer) returns 400 `code=consent_version_outdated` with the current version in the response body. Bump to force every org to re-consent on the next admin-UI mount; existing rows are never auto-upgraded (spec §3.5). | A stale value would block all consent writes; a mistakenly-bumped value would force surprise re-prompts. |
| `GOOGLE_CLIENT_ID` | yes for SSO | `""` | `.env` (OAuth client id) | unset | `.do/app.yaml` SECRET | yes | Google OAuth2 client id. | SSO endpoints 503; button (if forcibly shown) crashes the redirect. |
| `CAPTCHA_REQUIRED` | no | `false` | `.env` `false` | unset (defaults to `false`) | `.do/app.yaml` `true` | no | Master switch for the register bot gate. When `true`, `/api/v1/auth/register` refuses any request without a token verified by the provider. Exposed to the frontend via `/api/v1/auth/status` so a backend flip is a real rollback on the next page load. First-run setup (`user_count == 0`) bypasses the gate so bootstrap stays usable. | Bot signups not blocked when `false`. When `true` with `CAPTCHA_SECRET=""` the verify call fails closed with `REASON_MISCONFIGURED` and all registrations are refused. |
| `CAPTCHA_PROVIDER` | no | `"turnstile"` | `.env` | `.env` | `.do/app.yaml` | no | Label only today (no dispatch logic). Reserved for a second provider later. | Mislabeled events / metrics; no functional impact. |
| `CAPTCHA_SECRET` | yes when `CAPTCHA_REQUIRED=true` | `""` | `.env` (Cloudflare test secret `1x0000000000000000000000000000000AA`) | unset | `.do/app.yaml` SECRET (real Cloudflare siteverify secret) | yes | The provider's siteverify shared secret. Never logged. The Turnstile dev test secret always returns success on any token. | Empty in prod with `CAPTCHA_REQUIRED=true` means every register attempt is refused; rollback by flipping `CAPTCHA_REQUIRED` to `false`. |
| `CAPTCHA_VERIFY_URL` | no | `https://challenges.cloudflare.com/turnstile/v0/siteverify` | `.env` (default) | `.env` (default) | `.do/app.yaml` (same default) | no | Provider siteverify endpoint. Override only when swapping providers. | Wrong URL means 404 / 4xx and the gate fails closed. |
| `CAPTCHA_VERIFY_TIMEOUT_S` | no | `5.0` | `.env` (5) | `.env` (5) | `.do/app.yaml` (5) | no | httpx timeout for the siteverify POST. Below ~3s risks false negatives under provider latency spikes; above ~10s holds the request worker too long. | Too low: legit signups fail closed under load. Too high: backend worker tied up. |
| `CAPTCHA_EXPECTED_HOSTNAME` | no | `""` | `.env` empty (skipped in dev) | unset | `.do/app.yaml` (`app.thebetterdecision.com`) | no | Pins the `hostname` field of the verify response. Empty disables the check (provider's widget-domain allowlist still applies). | Tokens issued from a different origin are still accepted when empty. |
| `CAPTCHA_EXPECTED_ACTION` | no | `""` | `.env` empty | unset | `.do/app.yaml` (`register`) | no | Pins the `action` field of the verify response (set client-side via the Turnstile widget options). Defense-in-depth pairing for the hostname pin. | Tokens issued for a different action would be accepted when empty. |
| `BILLING_UI_ENABLED` | no | `false` | `.env` `false` | unset (defaults to `false`) | `.do/app.yaml` `false` (flip to `true` when payment platform is wired) | no | Master switch for the customer-facing plan / trial / billing surface. When `false`, the trial banner, settings Billing tab, and `/settings/billing` plan grid are hidden; `/settings/billing` renders an explanatory empty state. Admin / operator views under `/admin/*` and `/system/*` are unaffected. Exposed via `/api/v1/auth/status` so a backend flip becomes a customer-facing change on the next page load. Backend API gating (`/api/v1/subscriptions`, `/api/v1/plans`) is NOT in scope; UI-only hide. | Trial / billing UI visible to customers when `true` before payment is live. Flip to `false` to hide. |
| `FEATURE_REPORTS_V2` | no | `false` | `.env` `false` | unset (defaults to `false`) | `.do/app.yaml` `false` (flip to `true` in PR 4 once frontend + templates + sharing ship) | no | Master switch for the Reports v2 backend. When `false`, every route under `/api/v1/reports/*` returns 404 via a router-level dependency. Spec `specs/2026-05-22-reports-v2-flexible-canvas.md` §11. Frontend nav + routes are gated separately via `NEXT_PUBLIC_FEATURE_REPORTS_V2`; both sides flip together. | Reports surface unreachable from any client when `false`. Reports listing / CRUD / query unavailable. |
| `NEXT_PUBLIC_FEATURE_REPORTS_V2` | no | `false` | `.env` `false` | `.env` `false` | `.do/app.yaml` `false` (flip to `true` in PR 4) | no | Build-time gate for the Reports v2 frontend nav item and route shells. When not exactly `"true"`, the Reports menu entry is hidden and `/reports/*` routes are absent from the bundle. Pairs with the backend `FEATURE_REPORTS_V2` 404 gate. | Reports nav + routes hidden in the bundle when `false`. |
| `NEXT_PUBLIC_CAPTCHA_PROVIDER` | no | `"turnstile"` | `.env` | `.env` | `.do/app.yaml` | no | Build-time label inlined into the Next.js bundle. Reserved for a second provider later. | Mislabel only. |
| `NEXT_PUBLIC_CAPTCHA_SITE_KEY` | yes when `CAPTCHA_REQUIRED=true` | `1x00000000000000000000BB` (Turnstile always-pass test key) | `.env` (test key) | `.env` (test key) | `.do/app.yaml` (real Cloudflare site key) | no | Build-time inlined site key the widget renders against. Changing the value requires a frontend image rebuild. Widget render is gated on both `captcha_required` from `/api/v1/auth/status` AND a non-empty site key, so an empty value here means no widget renders even when the backend asks for one. | Empty in prod + `CAPTCHA_REQUIRED=true` → users can never obtain a token, every register attempt is rejected with `captcha_failed`. |
| `GOOGLE_CLIENT_SECRET` | yes for SSO | `""` | `.env` | unset | `.do/app.yaml` SECRET | yes | Google OAuth2 client secret. | SSO token exchange fails with `invalid_client`. |
| `BACKEND_CORS_ORIGINS` | yes | `http://localhost:3000` | `.env` (`http://localhost`) | unset | `.do/app.yaml` (`https://app.thebetterdecision.com`) | no | Comma-separated allowlist for `Access-Control-Allow-Origin`. | Browser blocks frontend XHR with CORS error. |
| `DEFAULT_PLAN_SLUG` | no | `pro` | `.env` (optional) | unset | inherit default | no | Default subscription plan slug at first-user creation. | Default `pro` used (beta posture). |
| `TRIAL_DURATION_DAYS` | no | `14` | `.env` (optional) | unset | inherit default | no | Trial duration assigned at first-user creation. | Default 14 used. |
| `OFX_PARSE_MAX_CONCURRENT` | no | `4` | `.env` (optional) | unset | inherit default | no | Global ceiling on simultaneous OFX statement parses (child processes) in one backend process. OFX files parse in a hard-killable child process so a pathological / adversarial upload cannot pin a CPU core after the request returns; this bounds how many can run at once. Per-replica (compute-only isolation, horizontally-scale-safe). Over the ceiling → HTTP 429 after `OFX_PARSE_QUEUE_WAIT_S`. | Default 4. Raise for more import throughput at the cost of more peak CPU/RAM; lower to protect a small box. |
| `OFX_PARSE_MAX_PER_ORG` | no | `2` | `.env` (optional) | unset | inherit default | no | Per-org ceiling on concurrent OFX parses, so one org cannot occupy every global slot and starve others. Over-cap → HTTP 429 immediately (non-blocking). Set `0` to disable the per-org cap (global cap still applies). Keep this STRICTLY BELOW `OFX_PARSE_MAX_CONCURRENT` to preserve cross-org fairness: setting it equal to or above the global ceiling lets a single org monopolize every parse slot. | Default 2. |
| `OFX_PARSE_QUEUE_WAIT_S` | no | `5.0` | `.env` (optional) | unset | inherit default | no | Bounded wait (seconds) for a free GLOBAL slot before returning HTTP 429, instead of queueing unbounded. `0` = reject immediately when full. The per-org cap always rejects immediately regardless of this value. | Default 5s. Too high holds request workers waiting; too low returns 429 under transient bursts. |
| `OFX_PARSE_TIMEOUT_S` | no | `10.0` | `.env` (optional) | unset | inherit default | no | Wall-clock parse budget (seconds). On overrun the parser child process is terminated (SIGTERM→SIGKILL) so CPU is actually reclaimed, and the request gets HTTP 400. | Default 10s. Raise to allow larger statements; too high lets a runaway hold a slot longer. |
| `OFX_MAX_ROWS` | no | `10000` | `.env` (optional) | unset | inherit default | no | Post-parse transaction-count cap for an OFX file → HTTP 413 on excess. Restored to 10 000 now that parsing is isolated + killable (was temporarily lowered to 2 000 as a pre-isolation DoS stopgap). | Default 10 000. Files above the cap must be split by date range. |
| `PFV_RUNTIME` | yes for prod | unset | unset | unset | `.do/app.yaml` (`app_platform`) | no | Tells `rate_limit.get_client_ip` to read `do-connecting-ip` unconditionally. | `audit_events.ip_address` records the DO ingress IP, not the user's IP. |
| `PFV_MIGRATE_OK_OFF_MAIN` | no | unset | shell / `.env` only when needed | unset | unset | no | Escape hatch for the lifespan + `./pfv migrate` branch guard. Allows migrations from a non-`main` checkout. | Without it, lifespan and `./pfv migrate` refuse to run on a feature branch. |

#### Backend MySQL container (local dev only)

These are consumed by the `mysql` service in `docker-compose.yml`, not by
the Python backend. They must match the credentials embedded in
`DATABASE_URL`.

| Variable | Required | Default | Local | Sensitive | Purpose |
|---|---|---|---|---|---|
| `MYSQL_ROOT_PASSWORD` | yes | `root_secret` | `.env` | yes (locally trivial) | MySQL root password (init only). |
| `MYSQL_DATABASE` | yes | `pfv2` | `.env` | no | Schema created at init. |
| `MYSQL_USER` | yes | `pfv2` | `.env` | no | App-level MySQL user created at init. Must match `DATABASE_URL`. |
| `MYSQL_PASSWORD` | yes | `pfv2_secret` | `.env` | yes (locally trivial) | `MYSQL_USER`'s password. Must match `DATABASE_URL`. |

Production MySQL runs on the `pfv-data-01` droplet; credentials live in
`DATABASE_URL` (SECRET) and not in any per-mysql-container env. See
`infra/MIGRATION.md`.

#### Backend seed helpers (`./pfv seed`)

Only consumed by `backend/seed.py`. Optional; defaults give the `demo /
demo1234` user documented in CONTRIBUTING.md.

| Variable | Default | Purpose |
|---|---|---|
| `SEED_USERNAME` | `demo` | Username of the seeded user. |
| `SEED_EMAIL` | `demo@example.com` | Email of the seeded user. |
| `SEED_PASSWORD` | `demo1234` | Password of the seeded user. |
| `SEED_FIRST_NAME` | `Demo` | First name of the seeded user. |
| `SEED_LAST_NAME` | `User` | Last name of the seeded user. |
| `SEED_ORG` | `Demo Household` | Org name created for the seeded user. |

### Frontend (Next.js)

Loaded at build time (`NEXT_PUBLIC_*`) or at server runtime (everything
else). `NEXT_PUBLIC_*` values are baked into the static JS bundle, so they
MUST be set at BUILD time in App Platform.

| Variable | Required | Default | Local | CI | Prod | Sensitive | Purpose | Failure mode if missing |
|---|---|---|---|---|---|---|---|---|
| `NEXT_PUBLIC_API_URL` | no | `""` | `.env` (empty for same-origin via nginx) | unset | `.do/app.yaml` (empty for same-origin via ingress) | no | Prefix prepended to fetch URLs in `lib/api.ts`. Empty means same-origin. | Cross-origin deployments lose API access if missing and not same-origin. |
| `NEXT_PUBLIC_SITE_URL` | no | empty (no canonical headers emitted) | `.env` (`https://app.thebetterdecision.com`) | unset | inherit `.env.example` or set explicitly | no | Canonical URL used in SEO metadata (sitemap, robots, OG image URLs). | Canonical tags and OG URLs are omitted. |
| `NEXT_PUBLIC_GOOGLE_SSO_ENABLED` | yes when SSO is wired | `false` | `.env` (`true` to show the button) | unset | `.do/app.yaml` (`true`) | no | `GoogleSSOButton`, `LoginPageBody`, and `RegisterPageBody` render the "Sign in with Google" button only when this is exactly the string `"true"`. | Button is hidden; users have no SSO entry point. |
| `NEXT_PUBLIC_APP_VERSION` | no | `dev` | unset | unset | unset (consider setting at build) | no | Stamped into feedback widget payloads as `app_version`. | Falls back to `dev`. |
| `BACKEND_INTERNAL_URL` | yes for RSC | unset | `docker-compose.yml` (`http://backend:8000`) | unset | `.do/app.yaml` RUN_TIME (`${backend.PRIVATE_URL}`) | no | Server-side fetch base URL for React Server Components (`forecast-plans`, `import/reconcile`, `lib/auth-server.ts`). | RSC pages cannot reach the backend; pages 500. |
| `HOSTNAME` | yes (DO) | unset | unset | unset | `.do/app.yaml` (`0.0.0.0`) | no | Forces Next standalone server to bind on all interfaces inside the container. | App Platform health check times out. |
| `NODE_ENV` | implicit | `production` in built image, `development` under `next dev` | container default | container default | container default | no | Switches CSP `unsafe-eval`, dev-only error logging, hot reload. | Production behavior assumed when unset (Next default). |

### Migrate job (DO PRE_DEPLOY)

A separate App Platform job that runs `python /app/scripts/migrate.py`
before any backend replica starts. Its env is independent from the
backend service.

| Variable | Required | Where | Purpose |
|---|---|---|---|
| `APP_ENV` | yes | `.do/app.yaml` (`production`) | Selects prod code paths. |
| `DATABASE_URL` | yes | `.do/app.yaml` SECRET | Migration target. Same encrypted `EV[...]` blob as the backend service. |
| `JWT_SECRET_KEY` | yes | `.do/app.yaml` SECRET | Required because `backend/app/config.py` instantiates `Settings()` at import; the JWT validator refuses the placeholder. Without this the migrate job crashes before alembic runs. See PR #202. |

---

## Feature flags and runtime modes

Most env vars above are config. The variables below are flags / switches
that change application behavior at runtime. Treat them as part of the
deploy contract.

| Variable | Component | What it does | Reference |
|---|---|---|---|
| `PFV_RUNTIME=app_platform` | backend | `get_client_ip` uses `do-connecting-ip` unconditionally so `audit_events.ip_address` records the real user IP, not the DO ingress peer. | PR #233, `project_audit_log_client_ip_bug.md` |
| `PFV_MIGRATE_OK_OFF_MAIN=1` | backend (lifespan + `./pfv migrate`) | Escape hatch for the branch guard that refuses to run migrations from a non-`main` checkout. Off-by-default. | `pfv` CLI, `backend/app/main.py` |
| `PFV_DEPDRIFT_SKIP=1` | `./pfv` CLI | Skips the host-vs-container `package-lock.json` SHA check on `./pfv start`. | `pfv` CLI line 48, PR #249 |
| `PFV_DEPDRIFT_HOST_HASH`, `PFV_DEPDRIFT_CONTAINER_HASH` | `./pfv` CLI (tests only) | Test seam for the drift guard. Not for human use. | `pfv` CLI |
| `NEXT_PUBLIC_GOOGLE_SSO_ENABLED=true` | frontend (build time) | Shows the "Sign in with Google" button on `/login`, `/register`, and step-up flows. Hidden otherwise. | PR #229 |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | backend | SQLAlchemy engine pool sizing. Defaults safe for single-replica; override when scaling HPA so `replicas * (pool_size + max_overflow)` stays under the managed-DB connection cap. | PR #251 (K8S-3) |
| `COOKIE_SECURE` | backend | When `true`, cookies are flagged `Secure` and browsers refuse to send them over HTTP. Must be `false` for local-dev HTTP and `true` for prod HTTPS. | `backend/app/config.py` |
| `AUTH_DEBUG_LOGGING=true` | backend | Enables the `auth.refresh.rejected` structured log event at every terminal-401 raise site in `/auth/refresh`. Each event carries a stable `reason` enum and 8-char SHA-256 prefixes of jti/sid (raw values are never logged). Flip on during incident triage; disable when done. The 401 still fires regardless of the flag — only the diagnostic emission is gated. | `backend/app/routers/auth.py` (`_log_refresh_rejected`), `backend/app/config.py` |
| `APP_ENV=production` | backend | Disables lifespan migrations (delegates to PRE_DEPLOY job), tightens MFA fallback (requires Redis), opens production-only auth paths. | `backend/app/main.py`, `backend/app/routers/auth.py` |
| `MAILGUN_API_KEY=""` | backend | When empty, `send_email` logs the recipient and subject and returns without calling Mailgun. Use for local dev. | `backend/app/services/email_service.py` |

---

## Secrets vs non-secrets

In DigitalOcean App Platform, the env-var entry's `type:` field controls
encryption.

### `type: SECRET` (encrypted at rest, surfaced as `EV[...]`)

These MUST be `SECRET` scope in `.do/app.yaml`:

- `DATABASE_URL`
- `REDIS_URL`
- `JWT_SECRET_KEY`
- `MFA_ENCRYPTION_KEY`
- `AI_CREDENTIAL_ENCRYPTION_KEY`
- `AI_CREDENTIAL_ENCRYPTION_KEY_PREV` (when rotating)
- `MAILGUN_API_KEY`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

The `EV[...]` blob in `.do/app.yaml` is encrypted with the app's per-app
key and is safe to commit. App Platform never surfaces plaintext after
the value is first set; even `doctl apps spec get` returns the encrypted
form. See the comment block in `.do/app.yaml`.

### Plain (`scope: RUN_AND_BUILD_TIME`, no `type:`)

These are committed as plaintext in `.do/app.yaml`:

- `APP_ENV`, `APP_NAME`, `LOG_LEVEL`
- `APP_URL`, `BACKEND_CORS_ORIGINS`
- `COOKIE_SECURE`
- `MAILGUN_DOMAIN`, `MAILGUN_REGION`, `EMAIL_FROM`
- `PFV_RUNTIME`
- `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_GOOGLE_SSO_ENABLED`
- `HOSTNAME`, `BACKEND_INTERNAL_URL`

---

## Deployment paths

### Local development

1. `cp .env.example .env`
2. Edit `.env`:
   - Set `JWT_SECRET_KEY` to a real 32+ char secret.
   - Leave Mailgun blank for console-logged email.
   - Leave Google blank unless you have real OAuth credentials.
   - `NEXT_PUBLIC_GOOGLE_SSO_ENABLED=false` unless you do (otherwise the
     button errors out on click).
3. `./pfv start`
4. `docker compose` reads `.env` via `env_file: .env` on the backend
   service. The frontend service gets `BACKEND_INTERNAL_URL` and
   `NEXT_PUBLIC_API_URL` inline from `docker-compose.yml`.

### CI (GitHub Actions)

Tests run inside `backend/tests/conftest.py`, which sets `DATABASE_URL`,
`APP_ENV=development`, and a fixture `JWT_SECRET_KEY` directly. CI does
NOT consume `.env`.

The deploy and smoke-tests jobs in `.github/workflows/release.yml` read
from GitHub Actions secrets:

- `DIGITALOCEAN_ACCESS_TOKEN` — `doctl` token for `digitalocean/app_action/deploy@v2`.
- `SMOKE_USERNAME`, `SMOKE_PASSWORD` — credentials for post-deploy smoke
  tests against `https://app.thebetterdecision.com`.

Add or rotate these in the repo's GitHub Settings → Secrets and variables
→ Actions.

### Production (DigitalOcean App Platform)

`.do/app.yaml` is the authoritative spec. Push with:

```bash
doctl apps update 3bcf70e8-2bae-4918-8297-ce430c79735e --spec .do/app.yaml
```

Any env var NOT listed in `.do/app.yaml` will be REMOVED from the live
app on the next push. This is the same failure mode that took down
production on 2026-04-25 when `JWT_SECRET_KEY` dropped to the placeholder.

The deploy workflow (`.github/workflows/release.yml`) uses
`app_spec_location: .do/app.yaml` and does NOT set `app_name` (see
[Spec-sync hazards](#spec-sync-hazards-digitalocean-app-platform)).

For build-time `NEXT_PUBLIC_*` vars (the SSO flag is the load-bearing
example), changing the spec triggers an App Platform rebuild because the
JS bundle must be regenerated. Expect a 2 to 3 minute auth-flow downtime
during the rebuild.

---

## Common failure modes

### "Google SSO button doesn't appear in production"

Symptom: `/login` and `/register` render without the "Sign in with
Google" button. Cause: `NEXT_PUBLIC_GOOGLE_SSO_ENABLED` is not set
(or not exactly the string `"true"`) at BUILD time. Fix: confirm the
variable is present in `.do/app.yaml`'s `frontend` `envs` block with
scope `RUN_AND_BUILD_TIME` and value `"true"`, then re-deploy
(`doctl apps update ... --spec .do/app.yaml`). A fresh build is required;
runtime-only changes do not bake into the JS bundle.

### "NEXT_PUBLIC_* env set in App Platform spec but not visible in client bundle"

Symptom: a `NEXT_PUBLIC_*` env is correctly declared in `.do/app.yaml`
with `scope: RUN_AND_BUILD_TIME`, `doctl apps update` succeeds, the
build finishes green, but `process.env.NEXT_PUBLIC_<NAME>` is still
`undefined` in the served JS bundle (so the feature it gates stays
hidden). Cause: `frontend/Dockerfile`'s build stage requires an explicit
`ARG NEXT_PUBLIC_<NAME>` line for every `NEXT_PUBLIC_*` env. Without
the `ARG`, the build stage does not see the env even though DO App
Platform's `RUN_AND_BUILD_TIME` scope makes it available to the build
CONTEXT. Next.js then inlines `undefined === "true"` as `false` and the
feature stays off. Fix: adding a new `NEXT_PUBLIC_*` env requires
(a) adding to `.do/app.yaml`, (b) adding an `ARG NEXT_PUBLIC_<NAME>`
line (and matching `ENV NEXT_PUBLIC_<NAME>=$NEXT_PUBLIC_<NAME>` to
promote it into the `npm run build` environment) to the build stage of
`frontend/Dockerfile`, (c) `doctl apps update <APP_ID> --spec .do/app.yaml`
to trigger a fresh build. This affects every `NEXT_PUBLIC_*` env, not
just the SSO flag.

### "Audit log shows ingress IP not user IP in production"

Symptom: `audit_events.ip_address` records `10.x.x.x` or DO ingress IPs.
Cause: `PFV_RUNTIME` is not set, so `get_client_ip` cannot trust
`do-connecting-ip`. Fix: confirm `PFV_RUNTIME=app_platform` is present
in `.do/app.yaml`'s `backend` `envs` block, then redeploy. Verify by
hitting any audited endpoint and inspecting the newest `audit_events`
row in prod MySQL.

### "Rate limit returns 500 instead of allowing the request"

Symptom: a hot endpoint (login, register, password reset) returns 500
when Redis is unreachable. Cause: slowapi raises on storage failure and
the current handler does not fail-open. Mitigation today: keep Redis
healthy. A separate hotfix to make slowapi fail-open is pending
coordinator decision. See PR #245 for context on the storage backend.

### "Frontend can't reach the API in production"

Symptom: every fetch returns 404 or hits the wrong host. Cause:
`NEXT_PUBLIC_API_URL` is set to a value other than the empty string
when the frontend is same-origin with the backend (the production
default). Fix: leave `NEXT_PUBLIC_API_URL=""` in `.do/app.yaml` so
fetch URLs become relative and route through the App Platform ingress
back to the `backend` component.

### "Server-side pages 500 with `ECONNREFUSED backend:8000`"

Symptom: `/forecast-plans` or `/import/[id]/reconcile` 500 on the server
side. Cause: `BACKEND_INTERNAL_URL` is missing or wrong. Fix: in dev,
the value lives in `docker-compose.yml` (`http://backend:8000`); in
prod, it MUST be `${backend.PRIVATE_URL}` with `scope: RUN_TIME` (App
Platform substitutes the component's private URL at runtime).

### "Email sending fails silently"

Symptom: password reset / verify / invite emails never arrive. Cause:
`MAILGUN_API_KEY` empty (dev-mode logger only), or
`MAILGUN_DOMAIN` / `MAILGUN_REGION` mismatch. Fix in prod: confirm all
three are populated in `.do/app.yaml` and that `MAILGUN_REGION=eu`
matches the configured Mailgun account region. A US-region key paired
with `MAILGUN_REGION=eu` returns Mailgun 401.

### "Backend refuses to boot with `JWT_SECRET_KEY` error"

Symptom: backend crashloops with `ValueError: JWT_SECRET_KEY must be set
to a real secret`. Cause: the env var is missing, still equals the
placeholder, or is shorter than 32 chars. Fix: regenerate
(`python -c "import secrets; print(secrets.token_urlsafe(64))"`) and set
in `.env` (local) or as a `SECRET` value in `.do/app.yaml` (prod). The
migrate PRE_DEPLOY job also needs this variable bound (PR #202).

### "Lifespan refuses to migrate on a feature branch"

Symptom: `./pfv start` boots the backend, but lifespan logs
`migrate.skipped reason=non_main_branch`. Cause: branch guard. Fix:
switch to `main` for migrations, or set `PFV_MIGRATE_OK_OFF_MAIN=1` in
`.env` if you genuinely need to run lifespan migrations from the branch.
See CLAUDE.md.

---

## Spec-sync hazards (DigitalOcean App Platform)

App Platform's live env is whatever `.do/app.yaml` last pushed. The push
path is one of:

1. `doctl apps update <APP_ID> --spec .do/app.yaml` (manual, owner-run).
2. `digitalocean/app_action/deploy@v2` with `app_spec_location: .do/app.yaml`
   (release workflow).

The action silently prefers `app_name` over `app_spec_location` when both
are set. `.github/workflows/deploy.yml` and `release.yml` intentionally
set ONLY `app_spec_location` to avoid this trap. See
`reference_do_spec_sync.md` in agent memory for the full incident log.

After merging any change to `.do/app.yaml`, the owner runs:

```bash
doctl apps update 3bcf70e8-2bae-4918-8297-ce430c79735e --spec .do/app.yaml
```

Without that step, edits to `.do/app.yaml` do not take effect until the
next release-classified merge (release.yml's deploy job runs only on
commits that semantic-release labels as a release). `chore`, `docs`, and
`refactor` commits do not auto-deploy.

Any env var NOT present in `.do/app.yaml` is REMOVED from the live app
on the next push. Treat the file as the complete env contract.

---

## Related files

- `.env.example` — copy-paste template with placeholder values and inline
  comments matching the Purpose column in this doc.
- `.do/app.yaml` — DigitalOcean App Platform spec. Authoritative for
  production env. Pushed via `doctl apps update`.
- `docker-compose.yml` — local-dev stack. Frontend env is inline; backend
  pulls from `.env` via `env_file`.
- `docker-compose.prod.yml` — single-host production compose (alternate
  to App Platform). Not used by the current production deploy.
- `backend/app/config.py` — pydantic-settings `Settings` model. Source of
  truth for backend env names, defaults, and validators.
- `backend/app/rate_limit.py` — `PFV_RUNTIME` consumer.
- `backend/app/security.py` — `JWT_SECRET_KEY` consumer.
- `backend/app/services/email_service.py` — Mailgun env consumer.
- `frontend/next.config.ts` — CSP build, `NEXT_PUBLIC_API_URL` origin
  allowlist.
- `frontend/components/auth/GoogleSSOButton.tsx` — gates on
  `NEXT_PUBLIC_GOOGLE_SSO_ENABLED`.
- `pfv` (CLI) — `PFV_DEPDRIFT_*` and `PFV_MIGRATE_OK_OFF_MAIN` consumers.
- `.github/workflows/release.yml`, `.github/workflows/deploy.yml` — GH
  Actions secrets and deploy invocation.
- `CONTRIBUTING.md` — local-dev setup walkthrough.
- `CLAUDE.md` — codebase-level conventions, including the lifespan
  branch guard reasoning.
