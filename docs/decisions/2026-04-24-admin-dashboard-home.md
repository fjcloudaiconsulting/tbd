# Admin Dashboard Home (L4.2)

Date: 2026-04-24
Status: Accepted
Scope: L4.2 admin dashboard home — first consumer of the L4.1 permission scaffold

## Context

L4.1 landed the platform permission system (`has_permission`, `require_permission`, `ROLE_PERMISSIONS`, `admin.view` candidates) but shipped with only `plans.manage` defined. Without a concrete consumer the scaffold stays theoretical.

L4.2 adds `/admin` — a superadmin-only dashboard showing headline KPIs and system health. This is the anchor for every subsequent admin surface (L4.3 org management, L4.4 user management, etc.), so the shape it establishes — aggregator endpoint, permission naming, frontend layout — gets reused.

## Scope constraints

Agreed before design to prevent scope creep:

- KPIs launch-MVP: total orgs, total users, active subscriptions, signups last 7d.
- Deferred unless already clean data exists: MRR surrogate, failed logins last 24h, queue depth, last deploy.
- System health MVP: DB reachable + query latency, Redis reachable + ping latency. Nothing else.
- One coarse new permission (`admin.view`). No fine-grained `orgs.view` / `users.view` yet — those land with L4.3 / L4.4.
- Frontend page reuses the existing `card` / `cardTitle` / typography utilities from `frontend/lib/styles.ts`. No separate admin design system.

## Decisions

### 1. One aggregator endpoint

```
GET /api/v1/admin/dashboard
```

Response shape:

```json
{
  "kpis": {
    "total_orgs": 17,
    "total_users": 42,
    "active_subscriptions": 12,
    "signups_last_7d": 3
  },
  "health": {
    "db":    { "ok": true,  "latency_ms": 4.0 },
    "redis": { "ok": false, "error": "timeout" }
  }
}
```

Why one endpoint, not one per KPI:

- A single page load should produce a single backend round-trip.
- KPIs are cheap (all `SELECT COUNT()`-shaped); no read is worth its own endpoint.
- The admin page doesn't need partial-refresh semantics in MVP.

### 2. Permission name: `admin.view`

Added to `Permission` Literal and `ALL_PERMISSIONS`. Coarse, exactly matches the endpoint's scope.

Why not skip permissions and gate on `user.is_superadmin` inline:

- L4.1 was built so subsequent admin routes use `require_permission`. Using inline `is_superadmin` on the very first customer of the scaffold defeats the point.
- `admin.view` stays stable across future admin-dashboard sub-pages that share the same gate.
- When / if `L4.8` adds a "support" role with read-only admin access, `admin.view` is the permission they inherit — no refactor.

Why not `orgs.view`, `users.view`, etc.:

- No such endpoints exist yet; defining them upfront violates the "add as you go" policy from L4.1's Decision 8.
- They land when L4.3 / L4.4 introduce the real endpoints they describe.

### 3. Separation of KPI failures from health-probe failures

Two independent `asyncio.gather` blocks in the service:

1. KPI counts (`SELECT COUNT`) — if any fail, the endpoint fails 500. These are load-bearing for the page, and failure means the DB is broken at a level the whole app can't tolerate.
2. Health probes (DB `SELECT 1` + Redis `PING`) — each wrapped in `try` / `except` with `asyncio.wait_for(..., timeout=PROBE_TIMEOUT_SECONDS)`. A stuck dependency can't stall the whole request; failures surface as `{ ok: false, error: "timeout" }` in the relevant cell.

`PROBE_TIMEOUT_SECONDS = 2.0` — short enough that a dead dependency doesn't gate the page, long enough to absorb normal jitter.

### 4. Frontend shape

- New page at `frontend/app/admin/page.tsx`, client component inside the authenticated shell.
- Four KPI cards in a responsive grid + two health rows with a pill indicator.
- `AppShell` header exposes an "Admin" link only when `user.is_superadmin === true`. Non-superadmins won't see it; they also can't reach the page via URL because the backend rejects the request.
- One `apiFetch` call on mount; error state surfaces via `extractErrorMessage`. No polling in MVP.

## Scope of the L4.2 PR

### Files added

- `backend/app/services/admin_dashboard_service.py` — the aggregator + health probes.
- `backend/app/routers/admin.py` — single route, gated by `Depends(require_permission("admin.view"))`.
- `frontend/app/admin/page.tsx` — the dashboard page.

### Files modified

- `backend/app/auth/permissions.py` — add `admin.view` to the `Permission` `Literal` and `ALL_PERMISSIONS`.
- `backend/app/main.py` — register the new router.
- `frontend/components/AppShell.tsx` (or equivalent nav component) — conditional "Admin" link.

### Explicitly out of scope

Everything downstream in L4 (all of it deliberately — each gets its own PR when scheduled):

- Org management, user management, per-org rate limits, audit log, analytics charts, /admin sub-routes.
- MRR computation, failed-login counters, queue depth, deploy metadata, drill-downs.
- Auto-refresh / polling, CSV export.
- `UserResponse.permissions` wire-up — not needed yet; `is_superadmin` alone tells the frontend whether to show the Admin link.

## Verification

Same pattern as L4.1 — no pytest harness, so manual verification plus syntax/import checks:

- `python3 -m py_compile` + `./pfv logs backend` auto-reload clean.
- `curl` matrix on `/api/v1/admin/dashboard`:
  - superadmin bearer → 200 with populated KPIs + both health cells `ok: true`.
  - non-superadmin bearer → 403 `{"detail":"Forbidden"}`.
  - anonymous → 403 (pre-existing `HTTPBearer` behaviour, acceptable per L4.1's decision record).
- Stop `pfv-redis-1`, re-run superadmin fetch → 200 with KPIs intact and `health.redis.ok === false`. Restart Redis.
- Frontend `npx tsc --noEmit` clean; browser click-through as superadmin shows the Admin link + dashboard renders.

## Follow-up

When L4.3–L4.10 land, each adds its own fine-grained permission to `ROLE_PERMISSIONS` alongside the feature. The `admin.view` gate established here is the shared "can see /admin at all" permission; per-resource permissions stack on top for specific actions.
