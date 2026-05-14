# The Better Decision

Personal finance management for people who actually want to understand where their money goes.

Track income and expenses across multiple accounts, set budgets per category, forecast future spending, import bank CSVs, and manage recurring transactions, all org-scoped so multiple users can share a household's finances.

## Features

- **Dashboard** with spending breakdown, budget progress, and forecast comparison charts
- **Transactions** with income, expenses, and linked account-to-account transfers
- **Hierarchical categories**, master categories for budgets, subcategories for tagging
- **Budgets** per category per billing period, with inter-budget transfers
- **Forecast plans**, editable income / expense plans with actual vs planned tracking
- **Recurring transactions**, templates that auto-generate future transactions
- **CSV import**, upload bank exports, preview with duplicate detection, map categories
- **Billing periods**, org-level month close dates with configurable cycle day
- **Multi-account**, checking, savings, credit cards, each with balance tracking
- **Authentication**, email / password, Google SSO, TOTP MFA with recovery codes and email fallback
- **Org-scoped**, all data isolated per organization, multi-user ready
- **Responsive**, works on desktop and narrow viewports (tablet, half-screen)

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS, Recharts |
| Database | MySQL 8.0 (self-hosted on a single DO droplet in production) |
| Cache | Valkey 8 / Redis-compatible (containerized in dev, self-hosted on the same droplet in production) |
| Auth | JWT (access + refresh), bcrypt, TOTP (pyotp), Google OAuth2 (with step-up for sensitive flows) |
| Email | Mailgun (production), structlog (development) |
| Proxy | nginx (development), DO App Platform ingress (production) |
| Landing (apex) | AWS S3 + CloudFront + ACM + IAM OIDC, separate from the app, see [`DEPLOYMENT.md`](DEPLOYMENT.md) |

## Quick Start

```bash
git clone https://github.com/flamarion/pfv.git && cd pfv
cp .env.example .env
./pfv start
```

Open [http://localhost](http://localhost). The first user to register becomes the superadmin.

**Seed mock data** (optional):

```bash
./pfv seed          # creates demo / demo1234 user with 100+ transactions
```

Full first-PR walkthrough (under 30 minutes from clone to push): [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

Every part of the project has a single authoritative document. Start with the row that matches your task.

### Getting started + day-to-day development

| Doc | When you need it |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | First-time contributor. 30-minute Quickstart, Conventional Commits + deploy gate, CI on your PR vs after merge, parallel-agent compose-isolation rule, first-PR decision tree. |
| [ENVIRONMENT.md](ENVIRONMENT.md) | Reference for every env var (backend, frontend, migrate job, CLI). Scope, default, sensitivity, deployment paths, failure modes. Source of truth for `.env`, `.do/app.yaml`, and GitHub Actions secrets. |

### Shipping + operations

| Doc | When you need it |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | What happens between `git push` and a live change. All CI/CD flows (PR lifecycle, automatic prod deploy, manual escape hatch, apex landing deploy), Terraform workspaces, migrations, what-triggers-what decision tree, per-pipeline rollback playbook, where to look when things break. Diagrams included. |

### Infrastructure

| Doc | When you need it |
|---|---|
| [infra/README.md](infra/README.md) | End-to-end topology of both clouds. DigitalOcean App Platform + data droplet (MySQL + Valkey) for the app surface, AWS (S3 + CloudFront + ACM + IAM OIDC) for the apex landing surface. TFC workspace layout, OIDC overview, DNS split between Cloudflare (app subdomain) and Route 53 (apex). |
| [infra/MIGRATION.md](infra/MIGRATION.md) | Production data-plane migration runbook. Used during the move from DO managed services to the self-hosted droplet, retained as the reference for any future host migration. |
| [infra/terraform/README.md](infra/terraform/README.md) | Day-2 reference for the `FlamaCorp/pfv` TFC workspace (DO data droplet). Variables, working dir, manual Confirm-and-Apply convention. |
| [infra/terraform/apex/README.md](infra/terraform/apex/README.md) | Day-2 reference for the `FlamaCorp/pfv-apex` TFC workspace (AWS apex landing). Bootstrap path B (static keys for one apply, then flip to OIDC), GitHub Actions deploy role, ACM in `us-east-1` rationale. |
| [infra/ansible/README.md](infra/ansible/README.md) | Configuration management for the data droplet (`pfv-data-01`). MySQL + Valkey roles, cloud-firewall coexistence, common role tasks. |

### Product + design

| Doc | When you need it |
|---|---|
| [PRODUCT.md](PRODUCT.md) | Target users, primary jobs-to-be-done, the operative product narrative. Background for design and UX decisions. |
| [BRAND.md](BRAND.md) | Brand kit: product name conventions, voice, palette, logo and favicon usage. Used when writing copy, building landing surfaces, or producing assets. |
| [DESIGN.md](DESIGN.md) | Design language and component conventions. Used when building or critiquing UI. |

### Working with AI in this repo

| Doc | When you need it |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Project-level guidance for Claude Code sessions. Stack, conventions, parallel-agent compose-isolation rule, migration branch guard, lifespan migration guard. |
| [AGENTS.md](AGENTS.md) | How agents (Claude or otherwise) interact with this repo. Rules of engagement, what is and is not allowed without explicit owner approval. |

## Architecture

```
Browser
  --> app.thebetterdecision.com (DO App Platform ingress)
        --> /api/*  --> backend  (FastAPI, port 8000)  --> MySQL  (pfv-data-01:3306)
        --> /*      --> frontend (Next.js, port 3000)  --> Valkey (pfv-data-01:6379)
  --> thebetterdecision.com (Route 53 -> CloudFront -> S3)
        --> static landing export (auth-free, no app code in bundle)
```

- **App backend** serves a REST API under `/api/v1/`. Stateless, horizontally scalable, ready for K8s.
- **App frontend** is a Next.js App Router build. All API calls use Bearer token auth with silent refresh.
- **Apex landing** is a separate Next.js static export built by `npm run build:apex`, deployed to S3 via GitHub Actions with OIDC role assume.
- **nginx** routes traffic in development. DO App Platform handles ingress for the app in production; CloudFront handles ingress for the apex.

For the full pipeline mechanics, see [DEPLOYMENT.md](DEPLOYMENT.md). For the cross-cloud topology, see [infra/README.md](infra/README.md).

## CLI

```bash
./pfv start             # build and start all services
./pfv stop              # stop all services
./pfv restart           # restart without rebuild
./pfv rebuild           # force rebuild (no cache)
./pfv reset             # destroy all data and start fresh
./pfv migrate           # run pending migrations (refuses off main without PFV_MIGRATE_OK_OFF_MAIN=1)
./pfv logs [service]    # view logs (backend, frontend, nginx, mysql, redis)
./pfv status            # container status
./pfv shell [service]   # shell into a container (default: backend)
./pfv seed              # populate with mock data
./pfv prod              # build and start in production mode
```

## API

Swagger UI: [http://localhost/api/docs](http://localhost/api/docs) (when running locally).

The full resource catalog and route conventions live in [CONTRIBUTING.md](CONTRIBUTING.md). Versioned under `/api/v1/`. Breaking changes go in `/api/v2/` while `v1` stays operational.

## License

Private project. Not open source.
