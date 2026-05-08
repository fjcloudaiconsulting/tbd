# Contributing to The Better Decision

This guide covers everything you need to develop, test, and deploy The Better Decision. Start with Quick Start if you're setting up for the first time, then refer to the architecture and API sections as needed.

## Prerequisites

- Docker & Docker Compose
- Git
- Node.js 18+ (only for local TypeScript checking — the app runs in containers)

## Quick Start

```bash
# 1. Clone and configure
git clone <repo-url> && cd pfv
cp .env.example .env

# 2. Start the dev stack (MySQL + Redis + Backend + Frontend + Nginx)
./pfv start

# 3. Open the app
open http://localhost
```

The first user to register becomes the org owner and superadmin.

## Seeding Mock Data

The seed script creates a realistic dataset: 5 accounts, 100+ transactions across 3 months, recurring templates, billing periods, and budgets.

**Default seed (creates a "demo" user):**

```bash
./pfv seed
# Login: demo / demo1234
```

**Custom seed (your own user):**

```bash
SEED_USERNAME=flamarion \
SEED_PASSWORD=abcd1234 \
SEED_EMAIL=flamarion@example.com \
SEED_FIRST_NAME=Flamarion \
SEED_LAST_NAME=Jorge \
SEED_ORG="FJ Consulting" \
./pfv seed
```

**Important:** The seed script will register the user if it doesn't exist, then log in and create data. If the user already exists, it logs in with the provided credentials and adds data to their org.

### Seed Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SEED_USERNAME` | `demo` | Username for the seeded user |
| `SEED_PASSWORD` | `demo1234` | Password |
| `SEED_EMAIL` | `demo@example.com` | Email address |
| `SEED_FIRST_NAME` | `Demo` | First name |
| `SEED_LAST_NAME` | `User` | Last name |
| `SEED_ORG` | `Demo Household` | Organization name |

## CLI Reference

| Command | Description |
|---------|-------------|
| `./pfv start` | Build and start all services (development) |
| `./pfv stop` | Stop all services |
| `./pfv restart` | Restart without rebuild |
| `./pfv rebuild` | Force rebuild (no cache) and start |
| `./pfv reset` | Destroy all data, rotate JWT secret, start fresh |
| `./pfv prod` | Build and start in production mode |
| `./pfv migrate` | Run pending database migrations |
| `./pfv logs [svc]` | View logs (backend, frontend, nginx, mysql, redis) |
| `./pfv status` | Show container status |
| `./pfv shell [svc]` | Open a shell in a service (default: backend) |
| `./pfv seed` | Populate with mock data |

---

## Architecture

### System Diagram

```
Browser --> nginx (:80) --> /api/*  --> backend (FastAPI :8000) --> MySQL (:3306)
                        --> /*      --> frontend (Next.js :3000)
                                        backend --> Redis (:6379)
```

In production (DigitalOcean App Platform), nginx is replaced by DO's built-in ingress routing.

### Backend (Python / FastAPI)

```
backend/app/
├── main.py                  # App factory, lifespan, CORS, router registration, 422 sanitizer
├── config.py                # pydantic-settings — all config from env vars
├── database.py              # Async SQLAlchemy engine + session factory
├── security.py              # JWT creation/decode, bcrypt, MFA token helpers
├── deps.py                  # FastAPI dependencies: get_db, get_current_user
├── logging.py               # structlog JSON logging + health check filter
├── rate_limit.py            # slowapi rate limiter (shared instance)
├── redis_client.py          # Async Redis singleton (MFA nonce, step-up state)
├── middleware/              # Pure ASGI middleware (request id / context)
├── models/                  # SQLAlchemy ORM models
│   ├── user.py              #   User, Organization, Role enum, password_set flag
│   ├── account.py           #   Account, AccountType
│   ├── category.py          #   Category (hierarchical), CategoryType
│   ├── category_rule.py     #   Per-org auto-categorization rules
│   ├── merchant_dictionary.py # Shared merchant -> category dictionary
│   ├── transaction.py       #   Transaction (income/expense/transfer, settled_date)
│   ├── recurring.py         #   RecurringTransaction templates
│   ├── billing.py           #   BillingPeriod (org-scoped)
│   ├── budget.py            #   Budget (per category per period)
│   ├── forecast_plan.py     #   ForecastPlan + ForecastPlanItem (with ItemSource)
│   ├── audit_event.py       #   Durable audit log (admin + sensitive ops)
│   ├── role.py              #   Custom roles + role_permissions
│   ├── invitation.py        #   Org invitation tokens
│   ├── subscription.py      #   Org subscription / trial state
│   ├── feature_override.py  #   Per-org plan-feature overrides
│   ├── org_data_reset_lock.py # Guard against concurrent org-data wipes
│   └── settings.py          #   OrgSetting (key-value per org)
├── schemas/                 # Pydantic request/response models (mirrors models/)
├── routers/                 # API route handlers
│   ├── auth.py              #   Login, register, MFA, Google SSO + step-up, password reset
│   ├── users.py             #   Profile update, password change
│   ├── accounts.py          #   CRUD accounts
│   ├── account_types.py     #   CRUD account types
│   ├── categories.py        #   CRUD categories (hierarchical, type-locked once used)
│   ├── transactions.py      #   CRUD transactions + transfers (settled_date period bucket)
│   ├── recurring.py         #   CRUD recurring templates + generation
│   ├── budgets.py           #   CRUD budgets + transfers between budgets
│   ├── forecast.py          #   Read-only computed forecast
│   ├── forecast_plans.py    #   CRUD editable forecast plans (MANUAL on public writes)
│   ├── import_router.py     #   CSV import: preview + confirm
│   ├── settings.py          #   Org settings, billing periods, billing cycle
│   ├── orgs.py              #   Org rename (owner-only, case-insensitive uniqueness)
│   ├── org_members.py       #   Org membership + invitations
│   ├── org_data.py          #   Org-data wipe / reset (audited + locked)
│   ├── plans.py             #   Plan catalog (read)
│   ├── subscriptions.py     #   Org subscription / trial state
│   ├── admin.py             #   Superadmin dashboard
│   ├── admin_orgs.py        #   Superadmin org management + override sweep
│   ├── admin_audit.py       #   Audit log query API
│   └── admin_roles.py       #   Custom role + permission editing
└── services/                # Business logic (called by routers)
    ├── billing_service.py       # Period management, resolve_period()
    ├── budget_service.py        # Budget queries with spending calculations
    ├── transaction_service.py   # Shared validation helpers, category-type guard
    ├── transaction_filters.py   # effective_period_date_expr() — COALESCE(settled_date, date)
    ├── recurring_service.py     # Generate transactions from templates
    ├── forecast_service.py      # Compute forecast from transactions + recurring
    ├── forecast_plan_service.py # Forecast plan CRUD with actual tracking
    ├── category_service.py      # Category CRUD with type compatibility checks
    ├── category_rules_service.py # Per-org auto-categorization rules
    ├── import_parser.py         # CSV parsing (delimiter, date, amount detection)
    ├── import_service.py        # Import preview + commit logic
    ├── email_service.py         # Mailgun (prod) / structlog (dev) email sender
    ├── mfa_service.py           # TOTP, QR codes, recovery codes, encryption
    ├── audit_service.py         # Persist audit_event rows (durable admin trail)
    ├── role_service.py          # Custom role + permission resolution
    ├── plan_service.py          # Plan catalog
    ├── subscription_service.py  # Trial creation, plan transitions
    ├── feature_service.py       # Plan + per-org feature override resolution
    ├── org_service.py           # Org rename + uniqueness checks
    ├── org_bootstrap_service.py # First-user-becomes-superadmin bootstrap
    ├── org_data_service.py      # Org-data wipe with snapshot + audit
    ├── org_reset_lock_service.py # Concurrency guard for wipes
    ├── invitation_service.py    # Org-member invitations
    ├── admin_dashboard_service.py # Superadmin dashboard aggregates
    ├── admin_orgs_service.py    # Superadmin org list / detail / override sweep
    ├── exceptions.py            # Domain exception types -> HTTP mappers in main.py
    └── date_utils.py            # Shared advance_date() for billing calculations
```

### Frontend (Next.js / TypeScript)

```
frontend/
├── app/                     # Next.js App Router pages
│   ├── dashboard/           #   Main dashboard with charts + summary
│   ├── transactions/        #   Transaction list (period bucketed by settled_date)
│   ├── accounts/            #   Account management
│   ├── recurring/           #   Recurring transaction templates
│   ├── budgets/             #   Budget management + transfers
│   ├── forecast-plans/      #   Editable forecast plans
│   ├── categories/          #   Category hierarchy management (type lock once in use)
│   ├── import/              #   CSV import wizard
│   ├── profile/             #   User profile editing
│   ├── settings/            #   /settings/security, /settings/billing, /settings/organization
│   ├── admin/               #   /admin (dashboard), /admin/orgs, /admin/audit, /admin/roles, /admin/settings
│   ├── login/               #   Login page
│   ├── register/            #   Registration page
│   ├── setup/               #   First-user / first-org setup
│   ├── accept-invite/       #   Org invitation acceptance
│   ├── mfa-verify/          #   MFA challenge during login
│   ├── forgot-password/     #   Request password reset
│   ├── reset-password/      #   Complete password reset
│   ├── verify-email/        #   Email verification
│   ├── auth/google/         #   Google SSO callback (login + step-up return)
│   ├── system/              #   Public system / status surface
│   ├── health/              #   Frontend health probe
│   ├── privacy/             #   Privacy Policy (public, GDPR-compliant)
│   └── terms/               #   Terms of Service (public)
├── components/
│   ├── AppShell.tsx         #   Sidebar + header + footer layout
│   ├── SettingsLayout.tsx   #   Sub-nav layout for /settings/*
│   ├── ThemeProvider.tsx    #   Dark / light theme provider
│   ├── auth/AuthProvider.tsx #  Auth context, login/logout/silent refresh
│   ├── admin/               #   Admin-only widgets (orgs table, audit table, roles)
│   ├── settings/            #   Settings sub-pages (security, billing, organization)
│   ├── transactions/        #   List + edit row + recurring promotion
│   ├── dashboard/           #   Dashboard tiles, charts, on-track verdict
│   ├── landing/             #   Public marketing surface
│   ├── system/              #   System status widgets
│   └── ui/                  #   Shared UI primitives
└── lib/
    ├── api.ts               #   Typed fetch wrapper with silent token refresh
    ├── types.ts             #   Shared TypeScript interfaces
    ├── styles.ts            #   Tailwind class constants (btnPrimary, card, input, etc.)
    ├── auth.ts              #   isAdmin() / role helpers
    ├── feature-catalog.ts   #   Plan-feature catalog mirror (kept in sync with backend)
    ├── format.ts            #   Currency / date formatters
    ├── logger.ts            #   Client+server structured JSON logger
    ├── pagination.ts        #   Shared list pagination helpers
    ├── site.ts              #   Public site URL helpers (canonical, OG)
    └── validation.ts        #   Shared client-side validation (mirrors backend/app/schemas)
```

### Key Design Decisions

- **All config via env vars** — pydantic-settings in backend, `NEXT_PUBLIC_` prefix in frontend
- **Stateless backend** — no in-memory state. JWT for auth, ready for horizontal scaling.
- **Migrations auto-run on startup** in dev. In production, they run as a `PRE_DEPLOY` job (App Platform) or initContainer (k8s) before the app starts.
- **First user is superadmin** — no seed data needed for bootstrapping.
- **Org-scoped data** — every query filters by `org_id`. Users only see their org's data.
- **Hierarchical categories** — master categories for budgets, subcategories as transaction tags. A category's `type` (income / expense / both) is enforced server-side on writes; once a category has been used the UI locks the type to keep historical aggregates honest.
- **Transfer category invariant** — transfer legs require a `CategoryType.BOTH` category. The system seeds a `Transfer` master category for this; arbitrary income / expense categories on transfer legs are rejected.
- **Billing periods** — org-level month close date. `settled_date` (or `COALESCE(settled_date, date)` for hand-keyed pending rows) determines which period a transaction counts against. The transactions list and aggregates both bucket on this effective period date.
- **Audit trail** — sensitive admin and org actions (org rename, org-data wipe, override sweep, role edits, etc.) write a row to `audit_events` and surface in `/admin/audit`. structlog still emits the same events to stdout, but the durable trail is the table.
- **Forecast plan source** — public writes always set `ItemSource.MANUAL`. Auto-population marks items as `RECURRING` or `HISTORY`; subsequent edits flip them to `MANUAL`. The HISTORY label surfaces as "Auto" in the UI.

---

## Authentication & Security

### Auth Flow

1. **Login** — `POST /api/v1/auth/login` with username/email + password, or Google SSO via `/api/v1/auth/google`
2. **MFA challenge** (if enabled) — returns `mfa_token`, user completes TOTP / recovery / email verification
3. **Tokens issued** — access token (15 min, in response body) + refresh token (7 day, httpOnly cookie)
4. **Silent refresh** — frontend auto-refreshes via `POST /api/v1/auth/refresh` on 401
5. **Absolute session lifetime** — sessions expire after a configurable max duration (default 30 days)

### SSO password security

Google-SSO users have `password_set=False` until they explicitly set a password. To prevent an unprompted password from being attached to a hijacked SSO session:

- **First password set** requires a Google **step-up** verification. The user re-authenticates with the same Google account, the backend issues a 5-minute single-use step-up token, and only then accepts the password write.
- **Reset password via email token** (the standard `/forgot-password` -> `/reset-password` flow) flips `password_set=True` on success, so future logins can use either Google SSO or the new password.
- **Step-up callbacks** redirect back through a server-side allowlist of `return_to` keys. Arbitrary URLs are rejected with `400 Malformed step-up state`.
- **Email change** also takes the step-up path and flips `password_set` back to `False` if the new email belongs to a different identity, forcing a re-set.

### MFA (Two-Factor Authentication)

- TOTP via authenticator app (Google Authenticator, Authy, 1Password, etc.)
- 8 single-use recovery codes (HMAC-SHA256 hashed, downloadable)
- Email fallback with 6-digit code (10-minute expiry)
- TOTP secrets encrypted at rest via Fernet (`MFA_ENCRYPTION_KEY` env var)
- Setup/disable via `/settings/security` page

### Rate Limiting

All limits are per client IP via slowapi's `get_remote_address`. In-memory storage is fine while the backend runs single-replica on DO App Platform; a Redis-backed store is deferred to the K8s migration.

| Endpoint | Limit |
|----------|-------|
| `POST /api/v1/auth/login` | 10/minute |
| `POST /api/v1/auth/register` | 5/hour |
| `GET /api/v1/auth/check-username` | 20/minute |
| `POST /api/v1/auth/verify-email` | 10/minute |
| `POST /api/v1/auth/resend-verification` | 3/hour |
| `POST /api/v1/auth/forgot-password` | 5/minute |
| `POST /api/v1/auth/mfa/verify` | 10/minute |
| `POST /api/v1/auth/mfa/recovery` | 10/minute |
| `POST /api/v1/auth/mfa/email-code` | 3/minute |
| `POST /api/v1/auth/mfa/email-verify` | 10/minute |

### Public Endpoints (no auth required)

`/health`, `/ready`, `/api/v1/auth/status`, `/api/v1/auth/login`, `/api/v1/auth/register`, `/api/v1/auth/refresh`, `/api/v1/auth/forgot-password`, `/api/v1/auth/reset-password`, `/api/v1/auth/verify-email`, `/api/v1/auth/google`, `/api/v1/auth/google/callback`, `/api/v1/auth/mfa/verify`, `/api/v1/auth/mfa/recovery`, `/api/v1/auth/mfa/email-code`, `/api/v1/auth/mfa/email-verify`

All other endpoints require a Bearer access token via `get_current_user` dependency.

---

## Environment Variables

### Required (Backend)

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | MySQL connection string | `mysql+aiomysql://user:pass@host:3306/db` |
| `JWT_SECRET_KEY` | HS256 signing key | `openssl rand -hex 32` |

### Optional (Backend)

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `development` | `development` or `production` |
| `APP_NAME` | `The Better Decision` | App name (used in TOTP issuer, emails) |
| `LOG_LEVEL` | `INFO` | Python log level |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token (idle timeout) |
| `SESSION_LIFETIME_DAYS` | `30` | Absolute max session duration |
| `COOKIE_SECURE` | `true` | Set `false` for local dev (HTTP) |
| `REDIS_URL` | _(empty)_ | Redis connection (`redis://...`) — used for MFA nonces and SSO step-up state |
| `MFA_ENCRYPTION_KEY` | _(empty)_ | Fernet key for TOTP secret encryption |
| `MAILGUN_API_KEY` | _(empty)_ | Mailgun API key (emails logged if empty) |
| `MAILGUN_DOMAIN` | _(empty)_ | Mailgun sending domain |
| `EMAIL_FROM` | `The Better Decision <noreply@thebetterdecision.com>` | From address for emails |
| `APP_URL` | `http://localhost` | Public URL (used in email links) |
| `GOOGLE_CLIENT_ID` | _(empty)_ | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | _(empty)_ | Google OAuth client secret |
| `BACKEND_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |

### Frontend

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_API_URL` | _(empty)_ | API base URL (empty = same-origin via nginx) |
| `HOSTNAME` | `0.0.0.0` | Next.js bind address |

### Generating Keys

```bash
# JWT secret
openssl rand -hex 32

# MFA encryption key (Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Database Migrations

Migrations run in one of three ways depending on environment:

- **Development** (`./pfv start`): the backend lifespan calls `_run_migrations()` on startup. Convenient for a single-container local stack.
- **Local prod simulation** (`./pfv prod`): a one-shot `migrate` service defined in `docker-compose.prod.yml` runs `alembic upgrade head` and exits; the backend then starts with `APP_ENV=production` which skips the startup migration.
- **Production (DO App Platform)**: a dedicated `PRE_DEPLOY` job defined in `.do/app.yaml` runs `alembic upgrade head` before any backend replica starts. The backend skips the startup migration because `APP_ENV=production`. **Operator note:** secrets (especially `DATABASE_URL`) must be configured against the `migrate` job component in the DO console — App Platform does not auto-inherit secrets across components.

```bash
# Create a new migration
docker compose exec backend alembic revision -m "description"

# Run pending migrations manually (dev)
./pfv migrate

# Check current migration state
docker compose exec backend alembic current
```

Migration files are in `backend/alembic/versions/` and follow sequential numbering (001, 002, ...).

---

## Development vs Production

| Aspect | Dev (`./pfv start`) | Prod (`./pfv prod`) |
|--------|-------|------|
| Frontend | Next.js dev server (hot reload) | Standalone build (`node server.js`) |
| Backend | uvicorn with `--reload` | uvicorn with 2 workers |
| Migrations | Auto-run on backend startup | Separate init service (runs first) |
| Volumes | Source code mounted | Immutable containers |
| Emails | Logged to console (structlog) | Sent via Mailgun |
| Entry point | nginx on port 80 | DO App Platform ingress |

---

## Branching & Pull Requests

- **Never push directly to `main`** — always branch + PR
- Feature branches: `feat/<name>`
- Fix branches: `fix/<name>`
- Merge to `main` triggers production deployment via GitHub Actions only when the commit is release-eligible (`feat`, `fix`, `perf`, `revert`). `chore`, `docs`, `refactor`, `test`, and similar commits are not auto-deployed; see the Deployment section for the manual escape hatch.

---

## Deployment

### DigitalOcean App Platform

The app is deployed on DO App Platform (Amsterdam region). MySQL 8 and Redis are **self-hosted** on a single dedicated DO droplet (`pfv-data-01`) in a private VPC; the App Platform components reach them over the VPC's private IPv4. Background and runbook live in `infra/README.md` and `infra/MIGRATION.md`.

**GitHub Actions workflows (`.github/workflows/`):**

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `release.yml` | Push to `main` (release-eligible commits only) | Runs semantic-release. If a new release is published, deploys `.do/app.yaml` to DO App Platform via `digitalocean/app_action/deploy@v2`, then runs `scripts/smoke-test.sh` against production. |
| `deploy.yml` | Manual (`workflow_dispatch`) | Emergency redeploy escape hatch. Same DO action and smoke-test job, but not auto-triggered. Use when an infra-only change (`chore(.do)`, `chore(infra)`, `chore(nginx)`) needs to ship without a version bump. |

The orchestration is intentional: semantic-release is the single arbiter of "should we ship". A path filter cannot tell a `chore(frontend)` apart from a real shipping change, which previously caused chore commits to redeploy production. Gating `deploy` on `new_release_published == 'true'` (a job output of the semantic-release step) ensures only release-eligible commits reach App Platform automatically. The naive `on: release: { types: [published] }` shortcut does not work because GitHub does not cascade workflow runs from `GITHUB_TOKEN`-created releases.

**Required GitHub repository secret:**

| Secret | Description |
|--------|-------------|
| `DIGITALOCEAN_ACCESS_TOKEN` | DO API token with read/write access to App Platform |

**App spec:** `.do/app.yaml` is the source of truth. Secrets are committed as App Platform's encrypted `EV[...]` blobs (only readable by DO) so they survive every deploy; any secret missing from the file is removed from the live app on push. The `vpc.id` block at the top wires the app to the data-droplet's VPC and must stay populated.

### Manual Deployment

The primary manual path is the `deploy.yml` workflow, which runs the same DO action and smoke-test job as the auto-deploy path:

```bash
# Trigger the manual workflow on main (preferred for chore(infra), chore(.do), etc.)
gh workflow run deploy.yml --ref main
```

If GitHub Actions is unavailable or you need to bypass it entirely (using `doctl` directly):

```bash
# Install doctl
brew install doctl
doctl auth init

# Push the spec (covers vpc, components, env, and secrets)
doctl apps update <app-id> --spec .do/app.yaml

# Or trigger a redeploy of the current spec
doctl apps create-deployment <app-id>
```

### Infrastructure as code

Production infra is split between App Platform (the application) and the data droplet (MySQL + Redis):

- **App Platform** is described by `.do/app.yaml` and pushed via the GH Actions workflow above.
- **Droplet, VPC, firewall, project attachment** are managed by Terraform under `infra/terraform/`. State and runs live in **HCP Terraform / Terraform Cloud** (workspace `FlamaCorp/pfv`). Workflow is VCS-driven: PRs touching `infra/terraform/**` get a speculative plan; merges to `main` create runs that require **manual Confirm & Apply** in the TFC UI. CLI `terraform plan` / `apply` is debug-only — never the routine path.
- **Droplet bootstrap** (Ubuntu hardening, MySQL, Redis, nightly mysqldump) is managed by Ansible under `infra/ansible/`.

### Infrastructure components

| Component | Service | Details |
|-----------|---------|---------|
| Backend | DO App Service | FastAPI, `basic-xxs`, port 8000 |
| Frontend | DO App Service | Next.js standalone, `basic-xxs`, port 3000 |
| Database | Self-hosted MySQL 8 on `pfv-data-01` | `s-1vcpu-2gb` droplet, ams3, private VPC |
| Cache | Self-hosted Redis on `pfv-data-01` | Same droplet, bound to private IP, `requirepass` set |
| Backups | Nightly `mysqldump` cron on the droplet (`/var/backups/mysql/`, 7-day retention) | DO droplet snapshots are **off** at the IaC level |

---

## API Documentation

- **Swagger UI:** http://localhost/api/docs (development)
- **OpenAPI spec:** http://localhost/api/openapi.json

All API routes are prefixed with `/api/v1/`. The API is organized by resource:

| Resource | Prefix | Description |
|----------|--------|-------------|
| Auth | `/api/v1/auth` | Login, register, MFA, Google SSO + step-up, password reset |
| Users | `/api/v1/users` | Profile, password change |
| Accounts | `/api/v1/accounts` | Bank accounts and balances |
| Account Types | `/api/v1/account-types` | Checking, savings, credit card, etc. |
| Categories | `/api/v1/categories` | Hierarchical income/expense categories |
| Transactions | `/api/v1/transactions` | Income, expenses, transfers (period bucketed by `settled_date`) |
| Recurring | `/api/v1/recurring` | Recurring transaction templates |
| Budgets | `/api/v1/budgets` | Per-category per-period budgets |
| Forecast | `/api/v1/forecast` | Computed forecast (read-only) |
| Forecast Plans | `/api/v1/forecast-plans` | Editable forecast plans |
| Import | `/api/v1/import` | CSV file import (preview + confirm) |
| Settings | `/api/v1/settings` | Org settings, billing periods, billing cycle |
| Orgs | `/api/v1/orgs` | Org rename (owner-only) and per-org actions |
| Org members | `/api/v1/orgs/members`, `/api/v1/orgs/invitations` | Membership and invitations |
| Org data | `/api/v1/orgs/data` | Org-data wipe / reset (audited, lock-guarded) |
| Plans | `/api/v1/plans` | Plan catalog |
| Subscriptions | `/api/v1/subscriptions` | Trial / subscription state |
| Admin | `/api/v1/admin` | Superadmin dashboard |
| Admin orgs | `/api/v1/admin/orgs` | Superadmin org management + override sweep |
| Admin audit | `/api/v1/admin/audit` | Audit log (durable trail) |
| Admin roles | `/api/v1/admin/roles` | Custom role + permission editing |

---

## Testing

### Backend (pytest)

The backend runs in the `backend` container; tests live in `backend/tests/` and run inside it:

```bash
# Full suite
docker compose exec backend pytest

# A single module or test
docker compose exec backend pytest tests/routers/test_auth.py
docker compose exec backend pytest tests/routers/test_auth.py::test_login_happy_path
```

`requirements-dev.txt` is installed in the dev image (`INSTALL_DEV=true` build arg, set in `docker-compose.yml`). Production / CI builds keep `INSTALL_DEV=false`.

### Frontend (vitest / jest)

```bash
# Full suite
docker compose exec frontend npm test

# A single test file
docker compose exec frontend npm test -- tests/lib/api.test.ts
```

### TypeScript type checking

```bash
docker compose exec frontend npx tsc --noEmit
# or, if you have node locally:
cd frontend && npx tsc --noEmit
```

### Manual smoke testing

The Swagger UI at http://localhost/api/docs is the fastest way to poke a single endpoint by hand. Browser testing covers UI flows; `curl` or httpie cover scripted checks.

---

## Troubleshooting

### Backend won't start

```bash
# Check logs
./pfv logs backend

# Common issues:
# - MySQL not ready: wait for health check, try ./pfv restart
# - Missing env var: check .env against .env.example
# - Migration error: ./pfv migrate
```

### Frontend build fails

```bash
# Check for TypeScript errors
cd frontend && npx tsc --noEmit

# Rebuild from scratch
./pfv rebuild
```

### Database issues (local dev)

```bash
# Connect to MySQL (uses values from .env)
docker compose exec mysql mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"

# Reset everything (destroys all data, rotates JWT secret)
./pfv reset
```

### MFA locked out (local dev)

If you lose access to your authenticator and recovery codes:

```bash
# Disable MFA directly in the local database
docker compose exec mysql mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
  -e "UPDATE users SET mfa_enabled=0, totp_secret=NULL, recovery_codes=NULL WHERE username='youruser';"
```
