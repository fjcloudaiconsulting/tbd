# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The Better Decision is a personal finance management application. FastAPI backend, Next.js + TypeScript frontend, MySQL database. Designed as a 12-factor app targeting Kubernetes for production.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2
- **Frontend:** Next.js 15 (App Router), React 19, TypeScript, Tailwind CSS, SWR
- **Database:** MySQL 8.0
- **Auth:** JWT (access + refresh tokens), bcrypt via passlib
- **Reverse proxy:** nginx (single entry point on port 80)
- **Dev environment:** Docker Compose + `./pfv` CLI

## Running Locally

```bash
cp .env.example .env    # First time only
./pfv start             # Build, start, run migrations
./pfv stop              # Stop all services
./pfv restart           # Restart without rebuild
./pfv rebuild           # Force rebuild (no cache)
./pfv reset             # Destroy all data and start fresh
./pfv migrate           # Run pending migrations
./pfv logs [service]    # View logs (backend, frontend, nginx, mysql)
./pfv status            # Container status
./pfv shell [service]   # Shell into a container (default: backend)
```

- App: http://localhost
- API: http://localhost/api/
- API docs (Swagger): http://localhost/api/docs
- In-app user manual: http://localhost/docs

## Common Commands

```bash
# Create a new migration
docker compose exec backend alembic revision -m "description"

# Rebuild after dependency changes
docker compose up --build -d backend
docker compose up --build -d frontend
```

## Seeding

For a repeatable local dataset (accounts, transactions, budgets, recurring templates), run `./pfv seed`. See the Seeding Mock Data section of CONTRIBUTING.md for the full workflow and the `SEED_*` environment variables that let you customize the seeded user.

## Architecture

```
Browser → nginx (:80) → /api/*  → backend (FastAPI :8000) → MySQL (:3306)
                      → /*      → frontend (Next.js :3000)
```

All frontend-to-backend communication uses Bearer token authentication. No exceptions.

### Backend Structure

```
backend/app/
├── main.py          # FastAPI app, lifespan, CORS, router registration
├── config.py        # pydantic-settings, all config from env vars
├── database.py      # async SQLAlchemy engine + session factory
├── security.py      # JWT encode/decode, bcrypt hash/verify
├── deps.py          # FastAPI dependencies: get_db, get_current_user
├── logging.py       # structlog JSON setup
├── models/          # SQLAlchemy ORM models
├── schemas/         # Pydantic request/response models
└── routers/         # API route handlers
```

### Frontend Structure

```
frontend/
├── app/             # Next.js App Router pages
├── components/      # React components by feature
└── lib/
    ├── api.ts       # Typed fetch wrapper with Bearer token + silent refresh
    └── types.ts     # Shared TypeScript types
```

## Key Conventions

- **All config via env vars** — pydantic-settings in backend, NEXT_PUBLIC_ prefix in frontend
- **Stateless backend** — no in-memory state, no filesystem dependencies. Ready for horizontal scaling.
- **Migrations run on startup** — backend auto-runs `alembic upgrade head` at boot. In production K8s, use an init container instead to avoid races across replicas.
- **First user is superadmin** — the first registered user gets `is_superadmin=True` automatically. No seed data needed.
- **Org-scoped data** — all user data is scoped to an organization. Every query must filter by org_id.
- **API versioning** — all API routes are prefixed with `/api/v1/`. New routers must use `APIRouter(prefix="/api/v1/{resource}")`. Breaking changes go in `/api/v2/` while v1 stays operational.
- **Auth on every endpoint** — use `get_current_user` dependency. Only /health, /ready, /api/v1/auth/login, /api/v1/auth/register, /api/v1/auth/refresh are public.
- **Enum values** — SQLAlchemy enums use `values_callable=lambda x: [e.value for e in x]` to store lowercase values in MySQL
- **Frontend has two Dockerfiles** — `Dockerfile.dev` for local dev (hot reload with volume mounts), `Dockerfile` for production (multi-stage standalone build, ~slim image)
- **nginx is the single entry point** — backend and frontend only expose ports internally. `/api/*` routes to FastAPI, everything else to Next.js. FastAPI's Swagger UI is served at `/api/docs` (with `/api/openapi.json`) so `/docs` is free for the public in-app user manual served by Next.js.


