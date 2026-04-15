# Contributing to PFV2

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
├── main.py                  # App factory, lifespan, CORS, router registration
├── config.py                # pydantic-settings — all config from env vars
├── database.py              # Async SQLAlchemy engine + session factory
├── security.py              # JWT creation/decode, bcrypt, MFA token helpers
├── deps.py                  # FastAPI dependencies: get_db, get_current_user
├── logging.py               # structlog JSON logging + health check filter
├── rate_limit.py            # slowapi rate limiter (shared instance)
├── models/                  # SQLAlchemy ORM models
│   ├── user.py              #   User, Organization, Role enum
│   ├── account.py           #   Account, AccountType
│   ├── category.py          #   Category (hierarchical), CategoryType
│   ├── transaction.py       #   Transaction (income/expense/transfer)
│   ├── recurring.py         #   RecurringTransaction templates
│   ├── billing.py           #   BillingPeriod (org-scoped)
│   ├── budget.py            #   Budget (per category per period)
│   ├── forecast_plan.py     #   ForecastPlan + ForecastPlanItem
│   └── settings.py          #   OrgSetting (key-value per org)
├── schemas/                 # Pydantic request/response models (mirrors models/)
├── routers/                 # API route handlers
│   ├── auth.py              #   Login, register, MFA, Google SSO, password reset
│   ├── users.py             #   Profile update, password change
│   ├── accounts.py          #   CRUD accounts
│   ├── account_types.py     #   CRUD account types
│   ├── categories.py        #   CRUD categories (hierarchical)
│   ├── transactions.py      #   CRUD transactions + transfers
│   ├── recurring.py         #   CRUD recurring templates + generation
│   ├── budgets.py           #   CRUD budgets + transfers between budgets
│   ├── forecast.py          #   Read-only computed forecast
│   ├── forecast_plans.py    #   CRUD editable forecast plans
│   ├── import_router.py     #   CSV import: preview + confirm
│   └── settings.py          #   Org settings, billing periods, billing cycle
└── services/                # Business logic (called by routers)
    ├── billing_service.py   #   Period management, resolve_period()
    ├── budget_service.py    #   Budget queries with spending calculations
    ├── transaction_service.py # Shared validation helpers
    ├── recurring_service.py #   Generate transactions from templates
    ├── forecast_service.py  #   Compute forecast from transactions + recurring
    ├── forecast_plan_service.py # Forecast plan CRUD with actual tracking
    ├── import_parser.py     #   CSV parsing (delimiter, date, amount detection)
    ├── import_service.py    #   Import preview + commit logic
    ├── email_service.py     #   Mailgun (prod) / structlog (dev) email sender
    ├── mfa_service.py       #   TOTP, QR codes, recovery codes, encryption
    └── date_utils.py        #   Shared advance_date() for billing calculations
```

### Frontend (Next.js / TypeScript)

```
frontend/
├── app/                     # Next.js App Router pages
│   ├── dashboard/           #   Main dashboard with charts + summary
│   ├── transactions/        #   Transaction list with filters
│   ├── accounts/            #   Account management
│   ├── recurring/           #   Recurring transaction templates
│   ├── budgets/             #   Budget management + transfers
│   ├── forecast-plans/      #   Editable forecast plans
│   ├── categories/          #   Category hierarchy management
│   ├── import/              #   CSV import wizard
│   ├── profile/             #   User profile editing
│   ├── settings/security/   #   Password, MFA, session lifetime
│   ├── admin/settings/      #   Org settings, billing periods
│   ├── login/               #   Login page
│   ├── register/            #   Registration page
│   ├── mfa-verify/          #   MFA challenge during login
│   ├── forgot-password/     #   Request password reset
│   ├── reset-password/      #   Complete password reset
│   ├── verify-email/        #   Email verification
│   └── auth/google/callback/ # Google SSO callback
├── components/
│   ├── AppShell.tsx         #   Sidebar + header + footer layout
│   ├── auth/AuthProvider.tsx #  Auth context, login/logout/refresh
│   └── ui/                  #   Shared UI components
└── lib/
    ├── api.ts               #   Typed fetch wrapper with silent token refresh
    ├── types.ts             #   Shared TypeScript interfaces
    ├── styles.ts            #   Tailwind class constants
    └── auth.ts              #   isAdmin() helper
```

### Key Design Decisions

- **All config via env vars** — pydantic-settings in backend, `NEXT_PUBLIC_` prefix in frontend
- **Stateless backend** — no in-memory state. JWT for auth, ready for horizontal scaling.
- **Migrations auto-run on startup** in dev. In production, they run before the app starts.
- **First user is superadmin** — no seed data needed for bootstrapping.
- **Org-scoped data** — every query filters by `org_id`. Users only see their org's data.
- **Hierarchical categories** — master categories for budgets, subcategories as transaction tags.
- **Billing periods** — org-level month close date. `settled_date` determines which period a transaction counts against.

---

## Authentication & Security

### Auth Flow

1. **Login** — `POST /api/v1/auth/login` with username/email + password
2. **MFA challenge** (if enabled) — returns `mfa_token`, user completes TOTP/recovery/email verification
3. **Tokens issued** — access token (15 min, in response body) + refresh token (7 day, httpOnly cookie)
4. **Silent refresh** — frontend auto-refreshes via `POST /api/v1/auth/refresh` on 401
5. **Absolute session lifetime** — sessions expire after a configurable max duration (default 30 days)

### MFA (Two-Factor Authentication)

- TOTP via authenticator app (Google Authenticator, Authy, 1Password, etc.)
- 8 single-use recovery codes (HMAC-SHA256 hashed, downloadable)
- Email fallback with 6-digit code (10-minute expiry)
- TOTP secrets encrypted at rest via Fernet (`MFA_ENCRYPTION_KEY` env var)
- Setup/disable via `/settings/security` page

### Rate Limiting

| Endpoint | Limit |
|----------|-------|
| `POST /api/v1/auth/login` | 10/minute |
| `POST /api/v1/auth/forgot-password` | 5/minute |
| `POST /api/v1/auth/mfa/verify` | 10/minute |
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
| `APP_NAME` | `PFV2` | App name (used in TOTP issuer, emails) |
| `LOG_LEVEL` | `INFO` | Python log level |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token (idle timeout) |
| `SESSION_LIFETIME_DAYS` | `30` | Absolute max session duration |
| `COOKIE_SECURE` | `true` | Set `false` for local dev (HTTP) |
| `REDIS_URL` | _(empty)_ | Redis/Valkey connection |
| `MFA_ENCRYPTION_KEY` | _(empty)_ | Fernet key for TOTP secret encryption |
| `MAILGUN_API_KEY` | _(empty)_ | Mailgun API key (emails logged if empty) |
| `MAILGUN_DOMAIN` | _(empty)_ | Mailgun sending domain |
| `EMAIL_FROM` | `PFV2 <noreply@pfv.app>` | From address for emails |
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

Migrations auto-run on backend startup in development. In production, they run as part of the container entrypoint before the app starts.

```bash
# Create a new migration
docker compose exec backend alembic revision -m "description"

# Run pending migrations manually
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
- Merge to `main` triggers production deployment via GitHub Actions

---

## Deployment

### DigitalOcean App Platform

The app is deployed on DO App Platform (Amsterdam region) with managed MySQL and Valkey (Redis-compatible).

**GitHub Actions workflows (`.github/workflows/`):**

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `deploy.yml` | Push to `main` | Deploys to production via `digitalocean/app_action/deploy@v2` |
| `preview.yml` | PR opened/updated | Creates ephemeral preview app, posts URL as PR comment |
| `delete-preview.yml` | PR closed | Cleans up preview app |

**Required GitHub repository secret:**

| Secret | Description |
|--------|-------------|
| `DIGITALOCEAN_ACCESS_TOKEN` | DO API token with read/write access to App Platform |

**App spec:** `.do/app.yaml` defines the app structure. Secrets use placeholder values — real secrets are configured in DO console and preserved across deployments.

### Manual Deployment

If you need to deploy without GitHub Actions:

```bash
# Install doctl
brew install doctl
doctl auth init

# Deploy current branch to the app
doctl apps create-deployment <app-id>
```

### Infrastructure

| Component | Service | Details |
|-----------|---------|---------|
| Backend | DO App Service | FastAPI, basic-xxs, port 8000 |
| Frontend | DO App Service | Next.js standalone, basic-xxs, port 3000 |
| Database | DO Managed MySQL 8 | Single node, ams3 |
| Cache | DO Managed Valkey 8 | Single node, ams3 |

---

## API Documentation

- **Swagger UI:** http://localhost/docs (development)
- **OpenAPI spec:** http://localhost/openapi.json

All API routes are prefixed with `/api/v1/`. The API is organized by resource:

| Resource | Prefix | Description |
|----------|--------|-------------|
| Auth | `/api/v1/auth` | Login, register, MFA, SSO, password reset |
| Users | `/api/v1/users` | Profile, password change |
| Accounts | `/api/v1/accounts` | Bank accounts and balances |
| Account Types | `/api/v1/account-types` | Checking, savings, credit card, etc. |
| Categories | `/api/v1/categories` | Hierarchical income/expense categories |
| Transactions | `/api/v1/transactions` | Income, expenses, transfers |
| Recurring | `/api/v1/recurring` | Recurring transaction templates |
| Budgets | `/api/v1/budgets` | Per-category per-period budgets |
| Forecast | `/api/v1/forecast` | Computed forecast (read-only) |
| Forecast Plans | `/api/v1/forecast-plans` | Editable forecast plans |
| Import | `/api/v1/import` | CSV file import (preview + confirm) |
| Settings | `/api/v1/settings` | Org settings, billing periods |

---

## Testing

### TypeScript Type Checking

```bash
cd frontend && npx tsc --noEmit
```

### Backend (manual)

No automated test suite yet. Test via:
- Swagger UI at http://localhost/docs
- Browser testing of full flows
- API calls via `curl` or httpie

Adding pytest infrastructure is on the technical debt backlog.

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

### Database issues

```bash
# Connect to MySQL
docker compose exec mysql mysql -upfv2 -ppfv2_secret pfv2

# Reset everything (destroys all data)
./pfv reset
```

### MFA locked out

If you lose access to your authenticator and recovery codes:

```bash
# Disable MFA directly in the database
docker compose exec mysql mysql -upfv2 -ppfv2_secret pfv2 \
  -e "UPDATE users SET mfa_enabled=0, totp_secret=NULL, recovery_codes=NULL WHERE username='youruser';"
```
