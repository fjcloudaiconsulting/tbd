# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The Better Decision is a personal finance management application. FastAPI backend, Next.js + TypeScript frontend, MySQL database. Built as a 12-factor app (Kubernetes-ready); currently deployed on DigitalOcean App Platform with a self-hosted MySQL + Redis data droplet (see the Production data plane note below).

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2
- **Frontend:** Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS, SWR
- **Database:** MySQL 8.4 LTS everywhere — dev, CI, and production (cut over 2026-08-19, TBD-360; production runs 8.4.11)
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
./pfv seed              # Seed a local dataset (deterministic; see Seeding below)
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

**Report-source catalog fixture (TBD-381).** The frontend derives every widget's
number format AND every filter control's visibility from `GET /api/v1/reports/sources`,
so its tests need the catalog. `frontend/tests/fixtures/report-sources.json` is
**generated**, never hand-written:

```bash
docker compose exec -T backend python -m scripts.regen_report_sources_fixture --stdout \
  > frontend/tests/fixtures/report-sources.json
```

⚠ `backend/tests/test_report_sources_frontend_contract.py` asserts it and **FAILS
rather than regenerating**. If it is red a source changed: re-run the generator,
**read the diff**, and confirm the frontend still formats and gates correctly.
Auto-regenerating restores exactly the silent drift it exists to stop — the
hand-written original disagreed with production in 16 places, one of them a live
right/wrong split, and omitted a whole source (which would have made any future
test of that source structurally vacuous, since an unknown dataset hits the
resolver's deliberate allow-everything branch). It lives in `backend/tests/` so
it rides the existing shards: no new CI job, no aggregate `needs:` wiring.

`backend/tests/test_period_status_frontend_contract.py` reads a fixture that lives
on the frontend side, so `docker-compose.yml` mounts
`./frontend/tests/fixtures:/app/frontend/tests/fixtures:ro` into the backend
service. A container built before that mount existed shows those tests red; run
`docker compose up -d --force-recreate backend` once to pick it up. The mount is
read-only on purpose — the fixture is regenerated deliberately, **on the host**,
with `cd backend && python -m scripts.gen_period_status_vectors` (that is the only
invocation that works; a script path puts `backend/scripts` on `sys.path` and
fails to import `app`).

**Backend shard timings (TBD-421).** `backend/.test_durations` balances the
`Backend Shard` matrix in CI. It is **generated, never hand-edited**, and it is
regenerated from a runner, not locally:

```bash
gh workflow run test-durations.yml --ref <branch>   # then commit the artifact
```

⚠ Regenerating it in the dev container does not work: the image copy is
root-owned while the container runs as uid 1001, so `--store-durations` runs the
entire suite and only then dies with `PermissionError`. Local timings are also
measurably not a uniform rescaling of runner timings, so a local harvest
balances CI worse than it appears to.

`backend/tests/test_test_durations_freshness.py` fails when the file drifts from
the collected suite (it went 130 commits stale once, costing ~110s per CI run).
The file is mounted into the backend container individually, like `seed.py` —
and because its update path is `git pull`, that mount is severed by the very
pull that refreshes it. If the container reports it missing, run
`docker compose up -d --force-recreate backend`.

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

For a local dataset (accounts, transactions, budgets, recurring templates), run `./pfv seed`. See the Seeding Mock Data section of CONTRIBUTING.md for the full workflow and the `SEED_*` environment variables.

**Deterministic, not idempotent (TBD-345).** For a given anchor date and RNG
seed, on a **fresh** database, the dataset is identical:

```bash
SEED_ANCHOR_DATE=2026-03-17 SEED_RANDOM_SEED=42 ./pfv seed
```

`SEED_ANCHOR_DATE` defaults to **today** — deliberately, against the ticket's
original ask for a fixed default. `billing_service.ensure_future_periods`
anchors its stubs to the open period's `start_date`, so a permanently-past
anchor would hand every developer an org whose open period is months behind the
calendar, and the current-month branch of the planner would go structurally
dead, so the credit-card `pending` state would never be demoed at all. Both
variables **raise** on a malformed value rather than falling back, so a caller
can never believe it pinned one and be wrong.

⚠ **Re-running against an already-seeded org APPENDS a second dataset** —
`POST /api/v1/accounts` has no duplicate-name check, so you get five more
accounts and another set of transactions. Any changed-anchor re-run also leaves
a **second open billing period**. Run `./pfv reset` first. Making the seed
idempotent needs a product ruling and is tracked separately.

The date/RNG geometry lives in pure planners (`resolve_anchor`, `resolve_rng`,
`plan_billing_periods`, `plan_transactions`) so it can be swept across anchors
in-process; `backend/tests/test_seed_determinism.py` does exactly that. ⚠ Note
`backend/seed.py` is mounted individually into the backend container — it sits
at the backend root, so no directory mount covers it, and without that mount
both `./pfv seed` and the seed tests read the copy baked into the image.

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
  - **CI validates migrations against real MySQL, and is the one caller that does NOT use the wrapper.** ⚠ It is the ONLY job in CI with a MySQL service at all — do not delete it (the next bullet says what it catches). Matrixed over `8.4` only since 2026-08-20 (TBD-415); the 8.0 leg went when production cut over. The `Migration Checks` job in `.github/workflows/test.yml` runs `alembic upgrade head` from an asserted-empty MySQL service on each matrix leg, asserts the stamped head, re-runs for idempotency, then boots the app and asserts `/ready`. It drives `alembic` directly because `backend/scripts/migrate.py` hardcodes `ALEMBIC_INI = "/app/alembic.ini"`, an in-container absolute path that does not exist on a bare runner. The job is folded into the `Backend Checks` aggregate, so branch protection needs only that one context name.
  - ⚠ **Everything else in CI runs on in-process aiosqlite**, so a MySQL-only defect (collation semantics, `VARCHAR` without a length, enum storage) is invisible to the shards — that is what this job exists to catch, and why `tests/migrations/test_sqlite_portability.py` disclaims covering it. ⚠ **Verifying a would-be CI command inside the backend container can pass while CI fails 100% of runs**: `backend/Dockerfile` sets `ENV PYTHONPATH=/app`, and nothing on a bare runner does, so anything importing `app` (including `alembic/env.py`) needs `PYTHONPATH` set explicitly in the workflow.
- **First user is superadmin and pre-verified** — the first registered user gets `is_superadmin=True` **and `email_verified=True`** automatically. No seed data needed. ⚠ The two are keyed on **different** predicates in `routers/auth.py::register` and they deliberately diverge: `email_verified` uses `is_first_user_setup` (`user_count == 0`, an empty `users` table), while `is_superadmin` uses `is_first_user` (`existing_superadmin == 0`). Anything new that keys off "the first user" has to pick the one it means — using `is_first_user` for a *bypass* grants it to a public self-signup on any install whose superadmins were demoted or deleted. `POST /auth/login` 403s unverified accounts unconditionally; there is no role, environment, or settings exemption at that gate, and the bootstrap is handled by minting verification at creation, never by relaxing the check.
- **Org-scoped data** — all user data is scoped to an organization. Every query must filter by org_id.
- **`users.pending_email` is an UNPROVEN claim, not an address (TBD-361)** — `PUT /users/me` records the claim and changes nothing else; `auth.verify_email` promotes it only when a token's `email` claim matches the stored value **exactly**, and only there do `users.email`, `email_verified` and `sessions_invalidated_at` move. It is cleared by exactly **four** writers: promotion, explicit cancel (`DELETE /users/me/pending-email`), overwrite by a later request, and promote-time conflict abort. Not by token expiry, `reset_password`, or deactivation.
  - ⚠ **Nothing that mails a credential may ever match on `pending_email`.** `forgot_password` deliberately looks up `user.email` only; widening it to the pending column mails a reset token to an unverified, self-asserted address, which is account takeover. The same rule holds for any future recovery path.
  - ⚠ **No unique index on it, deliberately.** A unique constraint on a self-asserted address does not prevent the collision that matters (a claim equal to somebody's *live* `users.email`) and hands any account an address-squatting primitive. Uniqueness is enforced against `users.email` at request time (advisory) and at promote time (binding, with `IntegrityError` → 409).
  - ⚠ **The session cutoff on promotion is NOT floored to whole seconds.** Every validator compares with a strict `<` and `create_access_token` already floors `iat`, so flooring leaves a token minted in the same second alive across an identity change. See `specs/2026-08-15-pending-email-two-phase-change.md`.
- **`linked_transaction_id` is not a transfer flag** — it has three writers: `transaction_service._link_pair` (bidirectional, a real transfer), `reconciliation_service._apply_match` (**one-way**, a reconcile match), and `transaction_service.unpair_transactions` (clears both sides). A non-null value therefore does **not** mean "transfer leg". Anything that means "these two rows are one transfer pair" must test mutuality via `transaction_filters.is_reciprocal_pair()`, never non-nullness; `transaction_filters.contributes_to_cached_balance()` is its sibling for balance reverts.
  - ⚠ **Deleting a row must demote its non-reciprocal inbound referrers.** The FK is `ON DELETE SET NULL`, so deleting a canonical row would otherwise leave its matched duplicate byte-identical to an ordinary transaction — re-entering both the balance reconstruction and every report while its amount is **not** in `accounts.balance`. `transaction_service._settle_batch_counters_and_demote_orphans()` marks those referrers `REJECTED` (already defined as "reverted, excluded, retained for audit"), which moves the discriminator into a column the FK cannot erase. There are exactly **four** transaction delete sites: `delete_transaction`, `bulk_delete_transactions`, `recurring_service._remove_pending_transactions` (all three route through the helper) and `org_data_service.wipe_org_data` (deliberately does not — it deletes the `import_batches` rows themselves, so no surviving counter needs settling). **A fifth delete path must route through the same helper**, or it silently reintroduces the orphan.
  - ⚠ **That helper also settles the DELETED rows' `ImportBatch` counters (TBD-311), which is why it is one function and not two.** A hard delete is not a state transition, and `_reconcile_one` — the only other counter maintainer — runs on transitions, so a deleted row's contribution used to vanish uncounted: `pending_count` stayed high forever and the batch could never auto-close. `close_batch_if_complete` **cannot** heal that: it returns early while `pending_count > 0`, so its recount fixes **under**-counts only and a stranded delete is an **over**-count. `row_count` and `accepted_count` are decremented too, because the reconcile screen renders `done = total_rows - pending_count` directly above a table built from the live rows. The counter write is one floored `UPDATE` per batch in ascending batch id; a second helper with its own `UPDATE import_batches` would re-create the self-inversion between concurrent deletes that shape exists to kill — invisibly, since no test can see a lock-order inversion.
    - ⚠ The early return in that helper guards on **both** classes (`if not demoted and not row_delta`). Guarding on `demoted` alone makes the counter fix dead on the commonest path — an ordinary delete with no inbound referrer — while every pre-existing fence in the area stays green, because they all build a matched pair. `test_matched_row_actions.py` F30/F31 build none, deliberately (F17/F18 are unrelated pre-existing fences in the same file).
  - ⚠ **The demotion is reported, never silent, on every path.** `DELETE /transactions/{id}` and the bulk sibling return `demoted_ids`, and since TBD-312 so do `POST /recurring/{id}/stop` and `DELETE /recurring/{id}` (both now carry real response models instead of `response_model=dict`). `REJECTED` is terminal and `reconciliation_state` is not settable through `TransactionUpdate`, so only direct SQL recovers it. The shared user-facing sentence lives in `frontend/lib/demotion.ts` and is used by both pages — do not re-word it per surface.
- **`category_id` on `GET /transactions` is master-includes-subs; the spending rollup is leaf-flat.** Two endpoints, two grouping semantics, and mixing them makes a drilldown sum to more than the slice that opened it. `GET /api/v1/transactions` defaults to `category_match="subtree"` (a deliberate 2026-05-13 regression guard), while `GET /api/v1/transactions/spending-by-category` groups by the row's **own** `category_id`. **Any drilldown from that rollup must pass `category_match=exact`** — measured live, a slice labelled 90.00 otherwise opens 250.00 of rows. `Literal["exact", "subtree"]` at `routers/transactions.py:104`.
  - Related: `period_start` on the rollup is a **hint, not a filter** — an unknown-but-valid value is silently substituted with the current period (no 404, no 422), and the call may auto-create and commit a `BillingPeriod`. Clients must read `period_start` back off the response rather than trust what they sent.
- **API versioning** — all API routes are prefixed with `/api/v1/`. New routers must use `APIRouter(prefix="/api/v1/{resource}")`. Breaking changes go in `/api/v2/` while v1 stays operational.
- **Auth on every endpoint** — use the `get_current_user` dependency. The public set is deliberately small and closed at **25 `(method, path)` pairs**: `/health`, `/ready`, the pre-auth and account-recovery half of `/api/v1/auth/*` (`status`, `check-username`, `login`, `register`, `refresh`, `verify`, `logout`, `forgot-password`, `reset-password`, `verify-email`, `resend-verification-public`, `google`, `google/callback`, `sso-stepup/callback`, and the four `mfa/*` **challenge** endpoints — NOT `mfa/setup`, `mfa/enable`, `mfa/disable`, `mfa/recovery-codes`, which are authenticated and interactive-session-gated), the two pre-account invitation routes (`GET /api/v1/orgs/invitations/preview`, `POST /api/v1/orgs/invitations/accept`), `/api/v1/public/founder-count`, `/api/v1/security/csp-report`, and `/api/v1/webhooks/mailgun` (signature-verified, not open). Only 10 of the 25 are truly open; the other 15 authenticate via a credential outside the dependency graph (refresh cookie, `mfa_token`, invitation JWT, reset/verify JWT, OAuth state cookie, Mailgun HMAC). **CONTRIBUTING.md's "Public endpoints" section is the authoritative list — do not add to it without a security review.** `backend/tests/auth/test_public_route_allowlist.py` enforces the set automatically, in both directions.
- **Reserved settings namespaces** — `RESERVED_SETTINGS_PREFIX = ("feature.", "orgpref.")` in `backend/app/routers/settings.py`. Both are managed by dedicated endpoints and are rejected by the generic org-settings write/delete paths. `feature.<name>` is the operator-facing gate; `orgpref.<name>` is the **tenant opt-out mask** an org admin writes for itself, applied inside `resolve_feature` so there is exactly one resolution path. An org admin never writes `feature.`.
  - ⚠ **Compare these case-insensitively.** Python's `startswith` is case-sensitive; MySQL's `utf8mb4_0900_ai_ci` collation is not. A guard that omits `.casefold()` is bypassable by capitalisation, and **SQLite-backed CI structurally cannot see it** — that was a live production auth bypass (TBD-322). All three call sites use `.casefold().startswith(...)`; keep it that way.
- **Enum values** — SQLAlchemy enums use `values_callable=lambda x: [e.value for e in x]` to store lowercase values in MySQL
- **Frontend has two Dockerfiles** — `Dockerfile.dev` for local dev (hot reload with volume mounts), `Dockerfile` for production (multi-stage standalone build, ~slim image)
- **nginx is the single entry point in dev** — backend and frontend only expose ports internally. `/api/*` routes to FastAPI, everything else to Next.js. FastAPI's Swagger UI is served at `/api/docs` (with `/api/openapi.json`) so `/docs` is free for the public in-app user manual served by Next.js. In production (DO App Platform) ingress takes nginx's role.
- **Production data plane is self-hosted** — MySQL 8 and Redis run on a single dedicated DO droplet (`<data-droplet>`) in a private VPC; App Platform reaches them over the VPC's private IPv4. Runbook lives in `infra/MIGRATION.md`. Terraform is VCS-driven via TFC (workspace `<tfc-org>/<data-workspace>`), with manual Confirm & Apply on merge.
- **Sensitive admin / org actions are audited** — org rename, org-data wipe, override sweep, role edits, etc. write to `audit_events` and surface in `/admin/audit`.
- **Squash-merge only** — merge commits and rebase merging are disabled on the repo. The squash subject is the **PR title** and the squash body is the **PR description**. This is why the PR title is the release gate: it is the string semantic-release parses.
- **CI is a required gate on `main` (TBD-347, 2026-08-10)** — `Backend Checks` and `Frontend Checks` are **required status checks**, pinned to the GitHub Actions app, with **`enforce_admins: true`**. A red suite can no longer be merged by anyone, including the maintainer. `strict` is deliberately **false** (branch-up-to-date is not forced; it would serialize every merge behind a full re-run). `required_pull_request_reviews` **is enabled** — 1 approval, with **bypass allowances** for the maintainer accounts (`flamarion`, `fjcloudai`) so a solo maintainer is not forced to self-approve through a second login.
  - ⚠ **An all-green PR still reports `mergeStateStatus: BLOCKED` until approved. That is the review requirement, NOT a wedged check** — do not reach for `scripts/breakglass-merge.sh`. The maintainer merges via the bypass. Measured on PR #647 (10/10 checks SUCCESS, still BLOCKED).
  - ⚠ **`.github/workflows/test.yml` has NO `paths:` filter, deliberately. Do not reintroduce one.** A required context that never reports blocks its PR **forever** on "Expected — waiting for status to be reported"; before this change, 4 of the last 12 merged PRs (docs-only) produced zero Actions check-runs. The repo is public, so runners are free and the filter conserved nothing.
    - ⚠⚠ **Since TBD-391 that ban is load-bearing for DEPLOYS, not just for PRs.** `release.yml` and `apex-deploy.yml` now wait for the `Test` run on the merge commit (`scripts/ci/await-test-run.sh`) before releasing or deploying. Having no `paths:` filter is what guarantees a Test run always exists for every push to `main`, which is what makes that wait terminate. Reintroducing one would no longer merely break PRs — it would **silently stop production deploys**, one 25-minute timeout at a time.
  - **The deploy interlock (TBD-391).** `release.yml` gates on the post-merge suite *before* `release`, not before `deploy`: semantic-release publishes an immutable tag ~7m41s before that suite reports, so gating only the deploy would still leave a release published for code whose tests then go red. The gate fails **closed** on failure/cancelled/timed-out/missing-run/API-error, produces no commit status (so it can never block a PR), and its escape hatch is `gh workflow run deploy.yml --ref main`, which stays deliberately ungated.
  - ⚠ **A new top-level job in `test.yml` must be wired into the `backend` aggregate's `needs:`.** Only the two aggregate contexts are required, so an unwired job reports an unrequired context: it goes red and the PR merges anyway. A guard step in the `backend` job enforces this and will fail the build naming the offending job.
  - **A genuinely wedged check is cleared with `scripts/breakglass-merge.sh <pr> "<reason>"`, never by hand** — the manual form is three commands where the third (re-arming enforcement) is enforced by nothing.
- **Jira-linked branches and commits** — the backlog is mirrored in Jira project `TBD` (`https://fjconsulting.atlassian.net/browse/TBD`). Put the issue key in the **branch name** (`TBD-179-sso-timeout`), in **commit messages**, and in the **PR title** (`fix(auth): … (TBD-179)`). Commit messages are what make GitHub Actions runs link as Jira "builds"; the branch name alone gets branches and PRs but no CI status.
  - ⚠ **Smart commits do NOT work in this repo. Do not rely on them.** Measured 2026-08-09 (TBD-212) and again across Sprint 9: squash-merge discards the branch commit **body**, so a `TBD-nnn #comment …` token never reaches the merged commit, **and** GitHub rewrites the committer to `noreply@github.com`, which cannot resolve to a Jira account. Both failures are silent — no error anywhere, and the comment simply never appears.
  - **Post the narrative via the Atlassian MCP after the merge instead**, as a normal issue comment. Same for status: transition the issue yourself.
  - The **repo-local `user.email`** matching the Jira account is still set and still worth keeping (it makes local commits attributable), but it does not rescue the smart-commit path — the committer rewrite happens at squash time, on GitHub's side.

## Design Context

Two root files carry the design source of truth; read them before any UI work:

- **`PRODUCT.md`** (strategy) — register (`product`), target users, product purpose, brand personality, anti-references (bank apps, spreadsheet skins), design principles (plan-first, line-item visibility, hierarchy-without-grids, quiet-by-default, status-is-data), and WCAG 2.2 AA commitments.
- **`DESIGN.md`** (visual system) — Stitch-format tokens in YAML frontmatter (colors, typography, rounded, spacing, components) plus the named rules that govern the app: *The One Brass Rule*, *Sidebar-Always-Navy*, *No Off-Token* (every color from `globals.css` theme tokens; raw Tailwind palette colors are CI-blocked by `frontend/scripts/check-design-tokens.sh`), *Brand-Surface Lock* (`frontend/lib/brand.ts` literals never theme-switch), and the typography/elevation/component rules. `DESIGN.json` is the renderable sidecar. Component primitives live in `frontend/lib/styles.ts`.


