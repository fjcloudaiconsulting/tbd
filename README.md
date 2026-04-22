# The Better Decision

Personal finance management for people who actually want to understand where their money goes.

Track income and expenses across multiple accounts, set budgets per category, forecast future spending, import bank CSVs, and manage recurring transactions — all org-scoped so multiple users can share a household's finances.

## Features

- **Dashboard** with spending breakdown, budget progress, and forecast comparison charts
- **Transactions** with income, expenses, and linked account-to-account transfers
- **Hierarchical categories** — master categories for budgets, subcategories for tagging
- **Budgets** per category per billing period, with inter-budget transfers
- **Forecast plans** — editable income/expense plans with actual vs. planned tracking
- **Recurring transactions** — templates that auto-generate future transactions
- **CSV import** — upload bank exports, preview with duplicate detection, map categories
- **Billing periods** — org-level month close dates with configurable cycle day
- **Multi-account** — checking, savings, credit cards, each with balance tracking
- **Authentication** — email/password, Google SSO, TOTP MFA with recovery codes and email fallback
- **Org-scoped** — all data isolated per organization, multi-user ready
- **Responsive** — works on desktop and narrow viewports (tablet, half-screen)

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Frontend | Next.js 15 (App Router), React 19, TypeScript, Tailwind CSS, Recharts |
| Database | MySQL 8.0 |
| Cache | Redis / Valkey |
| Auth | JWT (access + refresh), bcrypt, TOTP (pyotp), Google OAuth2 |
| Email | Mailgun (production), structlog (development) |
| Proxy | nginx (development), DO App Platform ingress (production) |

## Quick Start

```bash
git clone https://github.com/flamarion/pfv.git && cd pfv
cp .env.example .env
./pfv start
```

Open http://localhost. The first user to register becomes the superadmin.

**Seed mock data** (optional):

```bash
./pfv seed          # creates demo/demo1234 user with 100+ transactions
```

## Architecture

```
Browser --> nginx (:80) --> /api/*  --> backend (FastAPI :8000) --> MySQL (:3306)
                        --> /*      --> frontend (Next.js :3000)
                                        backend --> Redis (:6379)
```

- **Backend** serves a REST API under `/api/v1/`. Stateless, horizontally scalable.
- **Frontend** is a Next.js App Router SPA. All API calls use Bearer token auth with silent refresh.
- **nginx** routes traffic in development. In production, DigitalOcean App Platform handles ingress.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full architecture breakdown, environment variables, deployment guide, and development workflow.

## CLI

```bash
./pfv start             # build and start all services
./pfv stop              # stop all services
./pfv restart           # restart without rebuild
./pfv rebuild           # force rebuild (no cache)
./pfv reset             # destroy all data and start fresh
./pfv migrate           # run pending migrations
./pfv logs [service]    # view logs (backend, frontend, nginx, mysql, redis)
./pfv status            # container status
./pfv shell [service]   # shell into a container (default: backend)
./pfv seed              # populate with mock data
./pfv prod              # build and start in production mode
```

## API Documentation

Swagger UI is available at http://localhost/docs when running locally.

| Resource | Prefix | Description |
|----------|--------|-------------|
| Auth | `/api/v1/auth` | Login, register, MFA, SSO, password reset |
| Users | `/api/v1/users` | Profile, password change |
| Accounts | `/api/v1/accounts` | Bank accounts and balances |
| Categories | `/api/v1/categories` | Hierarchical income/expense categories |
| Transactions | `/api/v1/transactions` | Income, expenses, transfers |
| Recurring | `/api/v1/recurring` | Recurring transaction templates |
| Budgets | `/api/v1/budgets` | Per-category spending limits |
| Forecast | `/api/v1/forecast` | Computed forecast (read-only) |
| Forecast Plans | `/api/v1/forecast-plans` | Editable forecast plans |
| Import | `/api/v1/import` | CSV import (preview + confirm) |
| Settings | `/api/v1/settings` | Org settings, billing periods |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture details, environment variables, branching workflow, and deployment guide.

## Deployment

Production runs on DigitalOcean App Platform (Amsterdam). Merging to `main` triggers automatic deployment via GitHub Actions. See [CONTRIBUTING.md](CONTRIBUTING.md#deployment) for details.

## License

Private project. Not open source.
