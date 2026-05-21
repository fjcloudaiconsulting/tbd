---
name: ./pfv start dev-dependency-drift guard (XS) — SHIPPED #249
description: SHIPPED via PR #249 on 2026-05-13. Frontend lock-file drift between host and container now warns on ./pfv start/restart/status/logs frontend/shell frontend. Optional backend stretch goal not done.
type: project
originSessionId: 17ea612e-f064-4be7-b422-256d0da7b876
---
# `./pfv start` dev-dependency-drift guard — SHIPPED

**Status:** SHIPPED via PR #249, commit `9c9e2d6`, merged 2026-05-13.

## What landed
- `pfv` lines 32-92: `check_frontend_dep_drift()` — compares host `frontend/package-lock.json` sha256 vs container's `/app/package-lock.json`. Warns to stderr on mismatch with hints to `./pfv rebuild` or `docker compose exec frontend npm ci`. Skips silently when frontend container isn't running, when docker isn't available, or when neither `sha256sum` nor `shasum` is on PATH. Exit code unchanged.
- Hooks: `cmd_start`, `cmd_restart`, `cmd_status`, `cmd_logs frontend`, `cmd_shell frontend`.
- Test seam env vars: `PFV_DEPDRIFT_HOST_HASH`, `PFV_DEPDRIFT_CONTAINER_HASH`, `PFV_DEPDRIFT_SKIP`.

## Background (kept for context)

The dev frontend image bakes `node_modules` in at build time. Dev compose bind-mounts source but NOT `frontend/node_modules`, so a host `npm install` changes `package-lock.json` without that change reaching the running container. Silent failure mode until the new dep is actually imported (twice in May 2026: #203 stale CSS, #212 `server-only` MODULE_NOT_FOUND).

## Open stretch goal (not shipped, low priority)

Same drift check for `backend/requirements.txt` and `backend/requirements-dev.txt`. Backend has fewer dep changes per PR and Python imports are eager (container exits noisily on dep miss), so payoff is small. Pick up only if a backend dep drift incident actually bites.

## Why this memory is kept

To prevent re-picking the shipped task off the roadmap. The roadmap entry [./pfv dev-dep-drift guard] in `MEMORY.md` under "Open backlog items" needs to move to archive — done by the same session that finds this memory.
