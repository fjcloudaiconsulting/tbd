# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The Better Decision is a personal finance management application. FastAPI backend, Next.js + TypeScript frontend, MySQL database. Built as a 12-factor app (Kubernetes-ready); currently deployed on DigitalOcean App Platform with a self-hosted MySQL + Redis data droplet (see the Production data plane note below).

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2
- **Frontend:** Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS, SWR
- **Database:** MySQL 8.0
- **Auth:** JWT via PyJWT (access + refresh tokens), password hashing with `bcrypt` directly (no passlib)
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
./pfv seed              # Seed a repeatable local dataset (see Seeding below)
./pfv prod              # Run the production compose stack locally
./pfv logs [service]    # View logs (backend, frontend, nginx, mysql, redis)
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

# Backend tests (run inside the backend container)
docker compose exec backend pytest tests/...

# Frontend tests
docker compose exec frontend npm test -- tests/...

# TypeScript type-check
docker compose exec frontend npx tsc --noEmit
```

`backend/tests/test_period_status_frontend_contract.py` reads a fixture that lives
on the frontend side, so `docker-compose.yml` mounts
`./frontend/tests/fixtures:/app/frontend/tests/fixtures:ro` into the backend
service. A container built before that mount existed shows those tests red; run
`docker compose up -d --force-recreate backend` once to pick it up. The mount is
read-only on purpose — the fixture is regenerated deliberately, **on the host**,
with `cd backend && python -m scripts.gen_period_status_vectors` (that is the only
invocation that works; a script path puts `backend/scripts` on `sys.path` and
fails to import `app`).

### Running backend tests in parallel agent sessions

When dispatched as a parallel agent, NEVER run backend tests against the user's
default `pfv` docker compose stack. Always use an isolated compose project:

```bash
docker compose -p team-<unique-name> up -d backend mysql redis
docker compose -p team-<unique-name> exec backend pytest tests/...
```

Every `compose` and `docker exec` call must carry the same `-p team-<name>`
flag, on every command in the session. A single command that omits it falls
back to the default `pfv` project name and will write to the user's MySQL
volume. See `~/.claude/projects/-Users-flamarion-src-tbd/memory/reference_shared_mysql_volume_trap.md`
for the 2026-05-09 incident this rule prevents.

**Anchor every path to the worktree's absolute literal path.** Do NOT derive it
with `$(git rev-parse --show-toplevel)`: that resolves against the shell's
*current* directory, so once an agent's cwd drifts into a sibling worktree the
idiom silently returns the wrong root and "safely derived" absolute paths point
at another ticket's tree. Prefer `git -C /abs/path/to/worktree ...`, and `pwd`
before any shell write. This is not hypothetical — on 2026-07-30 an agent
appended 95 lines to a different ticket's test file this way; it was additive,
caught, and reverted before any commit. See
`memory/reference_rev_parse_toplevel_not_a_worktree_anchor.md`.

**Killing a `docker compose exec` kills only the local client — the process keeps
running inside the container.** Interrupting a long `pytest` leaves it alive,
consuming CPU and competing with whatever you start next; two agents on
2026-07-31 each lost a full-suite run this way, one of them to a run whose
source files were being mutated underneath it. Before trusting any timing or
result, scan for orphans with `/proc/*/cmdline` (`ps` is not installed in the
image) and **exclude your own scanning shell from the matches** — a scan that
matches only itself reads as "idle" and is how the second agent got caught. For
the same reason, never launch a full suite in the background and then edit or
inject into the same worktree: a result from a run whose tree changed mid-flight
is worthless in both directions.

`./pfv migrate` is for the user's local stack only. Do not invoke it from an
agent session; it has no `-p` flag and always targets the default project.

### Lifespan migration branch guard

`./pfv start`, `./pfv restart`, and `./pfv rebuild` boot the backend, whose
FastAPI lifespan calls `_run_migrations()` against the shared MySQL volume in
dev. To prevent the same drift class `./pfv migrate`'s CLI guard catches, the
lifespan reads `/app/.git/HEAD` and refuses to migrate when the host checkout
is on a non-main branch (or is detached / unreadable). If you genuinely need
to run lifespan migrations from a feature branch in dev, set
`PFV_MIGRATE_OK_OFF_MAIN=1` in `.env` or the shell. Default is to refuse,
since the same env var name pairs with `./pfv migrate`'s CLI guard.

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
├── rate_limit.py    # slowapi limiter + client-IP resolution (the ONLY IP helper)
├── models/          # SQLAlchemy ORM models
├── schemas/         # Pydantic request/response models
├── routers/         # API route handlers
├── services/        # Business logic (the bulk of the backend; ai_adapters/, ai_providers/, scheduler/)
├── auth/            # Permissions, PAT verification, feature catalog
├── middleware/      # request_context, security_headers
└── reports/         # Report engine: sources/, templates
```

The tree above names the load-bearing modules, not every file. `backend/scripts/migrate.py`
(outside `app/`) is the migrate wrapper every migration entrypoint drives.

### Frontend Structure

```
frontend/
├── app/             # Next.js App Router pages
├── components/      # React components by feature
├── tests/           # vitest suites (`npm test -- tests/...`)
├── scripts/         # build-apex.sh, check-design-tokens.sh (both CI gates)
└── lib/
    ├── api.ts       # Typed fetch wrapper with Bearer token + silent refresh
    ├── types.ts     # Shared TypeScript types
    ├── styles.ts    # Component style primitives (see DESIGN.md)
    └── hooks/       # Shared SWR data hooks (use-accounts, use-categories, ...)
```

Also non-exhaustive; `lib/` has ~30 more modules.

## Key Conventions

- **All config via env vars** — pydantic-settings in backend, NEXT_PUBLIC_ prefix in frontend
- **Stateless backend** — no in-memory state, no filesystem dependencies. Ready for horizontal scaling.
- **Migrations run on startup** — backend auto-runs `alembic upgrade head` at boot in dev via the FastAPI lifespan. The K8s init container, DO App Platform PRE_DEPLOY job, `docker-compose.prod.yml` migrate service, and `./pfv migrate` all go through the migrate wrapper at `backend/scripts/migrate.py`, which drives alembic per revision and emits structured JSON events (grep `migrate.start`, `migrate.step.start`, `migrate.complete`, `migrate.no_op`, `migrate.failed`).
- **First user is superadmin** — the first registered user gets `is_superadmin=True` automatically. No seed data needed.
- **Org-scoped data** — all user data is scoped to an organization. Every query must filter by org_id.
- **`linked_transaction_id` is not a transfer flag** — it has three writers: `transaction_service._link_pair` (bidirectional, a real transfer), `reconciliation_service._apply_match` (**one-way**, a reconcile match), and `transaction_service.unpair_transactions` (clears both sides). A non-null value therefore does **not** mean "transfer leg". Anything that means "these two rows are one transfer pair" must test mutuality via `transaction_filters.is_reciprocal_pair()`, never non-nullness; `transaction_filters.contributes_to_cached_balance()` is its sibling for balance reverts.
- **API versioning** — all API routes are prefixed with `/api/v1/`. New routers must use `APIRouter(prefix="/api/v1/{resource}")`. Breaking changes go in `/api/v2/` while v1 stays operational.
- **Auth on every endpoint** — use the `get_current_user` dependency. The public set is deliberately small and closed: `/health`, `/ready`, the pre-auth and account-recovery half of `/api/v1/auth/*` (`status`, `check-username`, `login`, `register`, `refresh`, `forgot-password`, `reset-password`, `verify-email`, `resend-verification-public`, `google`, `google/callback`, and the `mfa/*` challenge endpoints), `/api/v1/public/founder-count`, `/api/v1/security/csp-report`, and `/api/v1/webhooks/mailgun` (signature-verified, not open). **CONTRIBUTING.md's "Public endpoints" section is the authoritative list — do not add to it without a security review.**
- **Enum values** — SQLAlchemy enums use `values_callable=lambda x: [e.value for e in x]` to store lowercase values in MySQL
- **Frontend has two Dockerfiles** — `Dockerfile.dev` for local dev (hot reload with volume mounts), `Dockerfile` for production (multi-stage standalone build, ~slim image)
- **nginx is the single entry point in dev** — backend and frontend only expose ports internally. `/api/*` routes to FastAPI, everything else to Next.js. FastAPI's Swagger UI is served at `/api/docs` (with `/api/openapi.json`) so `/docs` is free for the public in-app user manual served by Next.js. In production (DO App Platform) ingress takes nginx's role.
- **Production data plane is self-hosted** — MySQL 8 and Redis run on a single dedicated DO droplet (`pfv-data-01`) in a private VPC; App Platform reaches them over the VPC's private IPv4. Runbook lives in `infra/MIGRATION.md`. Terraform is VCS-driven via TFC (workspace `FlamaCorp/pfv`), with manual Confirm & Apply on merge.
- **Sensitive admin / org actions are audited** — org rename, org-data wipe, override sweep, role edits, etc. write to `audit_events` and surface in `/admin/audit`.
- **Squash-merge only** — merge commits and rebase merging are disabled on the repo. The squash subject is the **PR title** and the squash body is the **PR description**. This is why the PR title is the release gate: it is the string semantic-release parses.
- **Jira-linked branches and commits** — the backlog is mirrored in Jira project `TBD` (`https://fjconsulting.atlassian.net/browse/TBD`). Put the issue key in the **branch name** (`TBD-179-sso-timeout`), in **commit messages**, and in the **PR title** (`fix(auth): … (TBD-179)`). Commit messages are what make GitHub Actions runs link as Jira "builds"; the branch name alone gets branches and PRs but no CI status.
  - **Smart commits** carry the narrative: `TBD-179 #comment <text>` in the commit **body** — never the subject, never the PR title (commands cannot span lines, and the subject is the release gate).
  - **Gotcha:** any `#word` token terminates the preceding `#comment` and is itself executed as a command. Put `#comment` **last** and keep its text free of `#` — write `PR 583`, not `#583`.
  - Smart commits resolve the committer by email and **fail silently** otherwise. This repo therefore sets a **repo-local** `user.email` matching the Jira account; don't revert it.

## Design Context

Two root files carry the design source of truth; read them before any UI work:

- **`PRODUCT.md`** (strategy) — register (`product`), target users, product purpose, brand personality, anti-references (bank apps, spreadsheet skins), design principles (plan-first, line-item visibility, hierarchy-without-grids, quiet-by-default, status-is-data), and WCAG 2.2 AA commitments.
- **`DESIGN.md`** (visual system) — Stitch-format tokens in YAML frontmatter (colors, typography, rounded, spacing, components) plus the named rules that govern the app: *The One Brass Rule*, *Sidebar-Always-Navy*, *No Off-Token* (every color from `globals.css` theme tokens; raw Tailwind palette colors are CI-blocked by `frontend/scripts/check-design-tokens.sh`), *Brand-Surface Lock* (`frontend/lib/brand.ts` literals never theme-switch), and the typography/elevation/component rules. `DESIGN.json` is the renderable sidecar. Component primitives live in `frontend/lib/styles.ts`.


