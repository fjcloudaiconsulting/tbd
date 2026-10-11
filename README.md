# The Better Decision

[![CI](https://github.com/fjcloudaiconsulting/tbd/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fjcloudaiconsulting/tbd/actions/workflows/ci.yml)
[![Release](https://github.com/fjcloudaiconsulting/tbd/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/fjcloudaiconsulting/tbd/actions/workflows/ci.yml)
[![backend coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Fflamarion%2Ff195ef7d7c927448bc7ccfae53fb6d58%2Fraw%2Ftbd-coverage-backend.json)](https://github.com/fjcloudaiconsulting/tbd/actions/workflows/ci.yml)
[![frontend coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Fflamarion%2Ff195ef7d7c927448bc7ccfae53fb6d58%2Fraw%2Ftbd-coverage-frontend.json)](https://github.com/fjcloudaiconsulting/tbd/actions/workflows/ci.yml)

Personal finance management for people who actually want to understand where their money goes.

Track income and expenses across multiple accounts, set budgets per category, forecast future spending, import bank CSVs, and manage recurring transactions, all org-scoped so multiple users can share a household's finances.

## Features

- **Customizable dashboard**, drag-and-drop widgets: spending breakdown, budget progress, forecast comparison, balances by type, credit-card utilization, loan payoff
- **Transactions** with income, expenses, and linked account-to-account transfers
- **Multi-account**, checking, savings, investment, credit cards (limits, utilization, billing cycles, statement alerts), and loans (payoff tracking), each with balance tracking
- **Hierarchical categories**, master categories for budgets, subcategories for tagging
- **Budgets** per category per billing period, with inter-budget transfers
- **Forecast plans**, editable income / expense plans with actual vs planned tracking
- **Low-balance warnings**, a per-account daily projection that flags the days an account is expected to go below zero, before it happens
- **Recurring transactions**, templates that auto-generate future transactions, with an optional instalment count so a fixed-length plan stops on its own
- **Reports**, a custom report builder over transactions, accounts, recurring, and net-worth-over-time, with charts, Sankey flows, and CSV export
- **Bank import**, upload CSV / OFX exports, preview with duplicate detection, map categories
- **Notifications**, in-app and email alerts (billing periods, credit-card statement close, and more)
- **AI assistance** (bring-your-own provider), transaction categorization, forecast refinement, budget rebalancing
- **Billing periods**, org-level month close dates with configurable cycle day
- **Authentication**, email / password, Google SSO, TOTP MFA with recovery codes and email fallback
- **Hide balances**, an eye toggle in the header that masks every money amount on screen, for when someone can see your screen
- **Org-scoped**, all data isolated per organization, multi-user ready
- **Responsive**, works on desktop and narrow viewports (tablet, half-screen)

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS, Recharts |
| Database | MySQL 8.4 LTS everywhere; in production a StatefulSet on the k3s cluster run from [aws-infra](https://github.com/fjcloudaiconsulting/aws-infra) |
| Auth | JWT (access + refresh), bcrypt, TOTP (pyotp), Google OAuth2 (with step-up for sensitive flows) |
| Email | Mailgun (production), structlog (development) |
| Proxy | nginx (development), Cloudflare in front of Traefik on k3s (production) |
| Landing (apex) | Cloudflare Worker `tbd-landing`, separate from the app, see [`DEPLOYMENT.md`](docs/operations/DEPLOYMENT.md) |

## Quick Start

```bash
git clone https://github.com/fjcloudaiconsulting/tbd.git && cd tbd
cp .env.example .env
./tbd start
```

Open [http://localhost](http://localhost). The first user to register becomes the superadmin.

**Seed mock data** (optional):

```bash
./tbd seed          # creates demo / demo1234 user with 100+ transactions
```

Full first-PR walkthrough (under 30 minutes from clone to push): [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

Every part of the project has a single authoritative document. Start with the row that matches your task.

### Getting started + day-to-day development

| Doc | When you need it |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | First-time contributor. 30-minute Quickstart, Conventional Commits + deploy gate, CI on your PR vs after merge, parallel-agent compose-isolation rule, first-PR decision tree. |
| [ENVIRONMENT.md](docs/operations/ENVIRONMENT.md) | Reference for every env var (backend, frontend, migrate job, CLI). Scope, default, sensitivity, deployment paths, failure modes. Source of truth for `.env` and GitHub Actions secrets; production values live in aws-infra. |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability. Private contact, scope, response time. |

### Shipping + operations

| Doc | When you need it |
|---|---|
| [DEPLOYMENT.md](docs/operations/DEPLOYMENT.md) | What happens between `git push` and a live change. All CI/CD flows (PR lifecycle, release and image promotion, Worker landing deploy), migrations, what-triggers-what decision tree, per-pipeline rollback playbook, where to look when things break. Diagrams included. |

### Infrastructure

Where and how TBD runs (k3s cluster, Flux, in-cluster MySQL, backups, Cloudflare) lives in [fjcloudaiconsulting/aws-infra](https://github.com/fjcloudaiconsulting/aws-infra). Its `docs/runbooks.md` and `docs/configuration-map.md` are the operations entry points.

### Product + design

| Doc | When you need it |
|---|---|
| [PRODUCT.md](docs/product/PRODUCT.md) | Target users, primary jobs-to-be-done, the operative product narrative. Background for design and UX decisions. |
| [BRAND.md](docs/product/BRAND.md) | Brand kit: product name conventions, voice, palette, logo and favicon usage. Used when writing copy, building landing surfaces, or producing assets. |
| [DESIGN.md](docs/design/DESIGN.md) | Design language and component conventions. Used when building or critiquing UI. |

## Architecture

```
Browser
  --> app.thebetterdecision.com (Cloudflare -> Traefik on k3s)
        --> /api/*  --> backend  (FastAPI, port 8000)  --> MySQL  (in-cluster)
        --> /*      --> frontend (Next.js, port 3000)
  --> thebetterdecision.com (Cloudflare Worker `tbd-landing`)
        --> static landing export (auth-free, no app code in bundle)
```

- **App backend** serves a REST API under `/api/v1/`. Stateless, horizontally scalable, ready for K8s.
- **App frontend** is a Next.js App Router build. All API calls use Bearer token auth with silent refresh.
- **Apex landing** is a separate Next.js static export built by `pnpm build:apex`, deployed as a Cloudflare Worker (`frontend/apex-worker/`) via GitHub Actions.
- **nginx** routes traffic in development. Traefik on the k3s cluster handles ingress for the app in production; Cloudflare handles ingress for the apex.

For the full pipeline mechanics, see [DEPLOYMENT.md](docs/operations/DEPLOYMENT.md). For the production topology, see [aws-infra](https://github.com/fjcloudaiconsulting/aws-infra).

## CLI

```bash
./tbd start             # build and start all services
./tbd stop              # stop all services
./tbd restart           # restart without rebuild
./tbd rebuild           # force rebuild (no cache)
./tbd reset             # destroy all data and start fresh
./tbd migrate           # run pending migrations (refuses off main without PFV_MIGRATE_OK_OFF_MAIN=1)
./tbd logs [service]    # view logs (backend, frontend, nginx, mysql)
./tbd status            # container status
./tbd shell [service]   # shell into a container (default: backend)
./tbd seed              # populate with mock data
./tbd prod              # build and start in production mode
```

## API

Swagger UI: [http://localhost/api/docs](http://localhost/api/docs) (when running locally).

The full resource catalog and route conventions live in [CONTRIBUTING.md](CONTRIBUTING.md). Versioned under `/api/v1/`. Breaking changes go in `/api/v2/` while `v1` stays operational.

## License

Private project. Not open source.
