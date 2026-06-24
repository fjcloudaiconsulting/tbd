# W4 Tier B — Phase 1 (Dashboard persistence + gated canvas shell) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stand up per-user dashboard-layout persistence and render the `/dashboard` through the existing Reports `Canvas` when a new **feature flag** is on — without disturbing today's live dashboard (flag off = unchanged). Proves the save/load/edit round-trip; the finance tiles get widgetized in Phase 2.

**Architecture:** New `DashboardLayout` model (mirrors `Report`), `GET/PATCH /api/v1/dashboard` gated behind `Feature.CUSTOM_DASHBOARD`, and a flag-switched dashboard page: flag off → the current fixed dashboard; flag on → the Canvas shell loading/saving `layout_json`. Reuses the Reports Canvas/WidgetShell/widgetKit/layout utils + the existing feature-gate system.

**Tech Stack:** FastAPI / SQLAlchemy async / Alembic / Pydantic v2 (backend); Next.js 16 / React 19 / TS (frontend); pytest + Vitest.

## Global Constraints

- **Prod-safe rollout:** the new dashboard is **OFF by default** (`Feature.CUSTOM_DASHBOARD`); the live dashboard is untouched when off. Mirror how `Feature.PLANS` is gated (`backend/app/services/feature_gate.py`, `require_feature`, `resolve_feature`, exposed in `/auth/status` `features` + the frontend `AuthProvider` features object).
- **Org + user scoped:** layout is per-user; `owner_user_id` from `current_user`, `org_id` from `current_user.org_id`, NEVER from the wire.
- **Strict layout validation:** reuse the Reports `layout_json`/`canvas_filters_json` Pydantic validators (`backend/app/schemas/report.py`). Per the #424 lesson: validate as a side-effect, return the blob VERBATIM — never `model_dump`-round-trip (strips real widget knobs).
- **Migration:** new revision chains after head `067` (down_revision `067_ix_transactions_org_type`). Confirm `alembic heads` before authoring. Parallel-agent backend tests use an isolated compose project (`-p team-<name>`) per CLAUDE.md.
- **No Off-Token** (frontend); **`npm run lint`** in frontend verify (eslint `no-explicit-any` is CI-gated) → [[reference_eslint_ci_gate_misses]]. No `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- **Templates to read** (don't reinvent): `backend/app/models/report.py`, `backend/app/routers/reports.py`, `backend/app/schemas/report.py`, `backend/app/services/feature_gate.py`, `frontend/lib/reports/api.ts`, `frontend/app/reports/[id]/page.tsx` (load/edit/save pattern), `frontend/components/reports/{Canvas,WidgetShell,widgetKit}.tsx`, `frontend/components/auth/AuthProvider.tsx`.

---

### Task 1: `DashboardLayout` model + migration

**Files:**
- Create: `backend/app/models/dashboard.py`
- Create: `backend/alembic/versions/068_dashboard_layout.py`
- Modify: `backend/app/models/__init__.py` (export, if models are registered there)
- Test: `backend/tests/models/test_dashboard_layout.py` (create) or fold into the router test

**Interfaces:**
- Produces: `DashboardLayout` ORM — `id`, `owner_user_id` (FK users, **unique**, indexed), `org_id` (FK orgs, indexed), `layout_json` (JSON), `canvas_filters_json` (JSON), `schema_version` (int, default 1), `created_at`, `updated_at`. One layout per user (v1).

- [ ] **Step 1: READ `backend/app/models/report.py`** for the column/JSON/timestamp conventions; mirror them.
- [ ] **Step 2: Write the model** `backend/app/models/dashboard.py` per the interface (unique constraint on `owner_user_id`; FKs with the project's ondelete convention — match how Report scopes org/user).
- [ ] **Step 3: Confirm head + author migration.** `docker compose exec backend alembic heads` (expect `067…`). Create `068_dashboard_layout.py` with `down_revision = "067_ix_transactions_org_type"`, `create_table("dashboard_layouts", …)` + the unique index on `owner_user_id`; full `downgrade` drops it. READ migration `066`/`067` for the project's exact op style.
- [ ] **Step 4: Apply on the test DB + write a model test.** A test that inserts a row, enforces the `owner_user_id` uniqueness, and round-trips the JSON columns. Run: `docker compose exec backend pytest tests/models/test_dashboard_layout.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**
```bash
git add backend/app/models/dashboard.py backend/alembic/versions/068_dashboard_layout.py backend/app/models/__init__.py backend/tests/models/test_dashboard_layout.py
git commit -m "feat(dashboard): DashboardLayout model + migration 068"
```

---

### Task 2: `Feature.CUSTOM_DASHBOARD` gate + dashboard schemas

**Files:**
- Modify: `backend/app/services/feature_gate.py` (add the enum member + default OFF)
- Modify: `backend/app/routers/auth.py` (expose `custom_dashboard` in `/auth/status` `features`)
- Create: `backend/app/schemas/dashboard.py`
- Test: extend a feature-gate test + add schema validation tests

**Interfaces:**
- Produces: `Feature.CUSTOM_DASHBOARD` (resolves OFF unless org/system override/env on); `/auth/status` `features.custom_dashboard: bool`; `DashboardLayoutOut` + `DashboardUpdate` schemas reusing `LayoutJson`/`CanvasFilters` validation from `schemas/report.py`.

- [ ] **Step 1: READ `feature_gate.py`** (how `Feature.PLANS` is defined + its default) and `auth.py:208-212` (where `reports`/`plans` flags are resolved into the status payload).
- [ ] **Step 2: Add the gate.** Add `CUSTOM_DASHBOARD` to the `Feature` enum with the same default-OFF semantics as `PLANS`. Add `"custom_dashboard": await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id, db)` to the `/auth/status` features dict.
- [ ] **Step 3: Schemas.** `backend/app/schemas/dashboard.py`: `DashboardLayoutOut` (`id, owner_user_id, org_id, layout_json, canvas_filters_json, schema_version, created_at, updated_at`) and `DashboardUpdate` (`layout_json?`, `canvas_filters_json?`) — reuse the SAME validators Reports uses (extract a shared validator from `schemas/report.py` if it isn't already importable; validate-and-return-verbatim, no `model_dump` round-trip).
- [ ] **Step 4: Tests.** Feature-gate test: `custom_dashboard` defaults OFF, flips with an override. Schema test: a knob-bearing `layout_json` round-trips verbatim (guard the #424 regression). Run the relevant pytest.
- [ ] **Step 5: Commit.**
```bash
git add backend/app/services/feature_gate.py backend/app/routers/auth.py backend/app/schemas/dashboard.py backend/tests/
git commit -m "feat(dashboard): CUSTOM_DASHBOARD feature gate + dashboard schemas"
```

---

### Task 3: `GET`/`PATCH /api/v1/dashboard` endpoints

**Files:**
- Create: `backend/app/routers/dashboard.py` (`APIRouter(prefix="/api/v1/dashboard")`, gated)
- Modify: `backend/app/main.py` (register the router)
- Test: `backend/tests/routers/test_dashboard.py` (create)

**Interfaces:**
- Consumes: Task 1 model, Task 2 gate + schemas.
- Produces: `GET /api/v1/dashboard` → caller's `DashboardLayoutOut`, **auto-creating** a default layout (server-side `DEFAULT_DASHBOARD_LAYOUT`, a minimal valid `LayoutJson` for Phase 1 — e.g. one KPI widget; real tiles arrive Phase 2) on first access. `PATCH /api/v1/dashboard` → owner-only update of `layout_json`/`canvas_filters_json`, validated.

- [ ] **Step 1: READ `backend/app/routers/reports.py`** for the auth dep, gate dep, validation→422 mapping, and `db` patterns.
- [ ] **Step 2: Write the failing router tests** (`test_dashboard.py`): (a) GET auto-creates + returns the default when none exists; (b) GET returns the existing row on second call (no duplicate); (c) PATCH owner updates layout, persists, round-trips verbatim; (d) a second user's GET is isolated (per-user scoping); (e) feature gate OFF → 403; (f) unknown body key → 422 (`extra="forbid"`). Mirror an existing reports router test's fixtures + feature-gate-on setup.
- [ ] **Step 3: Run, verify fail.** `docker compose exec backend pytest tests/routers/test_dashboard.py -v` → FAIL (router not registered).
- [ ] **Step 4: Implement** the router (gated by `require_feature(Feature.CUSTOM_DASHBOARD)`, `@limiter.limit` consistent with reports, auth dep, `org_id`/`owner_user_id` from `current_user`); register in `main.py`. Define `DEFAULT_DASHBOARD_LAYOUT` (minimal valid layout for Phase 1).
- [ ] **Step 5: Run, verify pass.** Then a broad `docker compose exec backend pytest tests/ -q` to confirm no regression from the new model/migration/router.
- [ ] **Step 6: Commit.**
```bash
git add backend/app/routers/dashboard.py backend/app/main.py backend/tests/routers/test_dashboard.py
git commit -m "feat(dashboard): GET/PATCH /api/v1/dashboard (gated, per-user, auto-create default)"
```

---

### Task 4: Frontend dashboard API + feature flag plumbing

**Files:**
- Create: `frontend/lib/dashboard/api.ts`, `frontend/lib/dashboard/types.ts`
- Modify: `frontend/components/auth/AuthProvider.tsx` (+ wherever the `features` type lives) to surface `customDashboard`
- Test: `frontend/tests/lib/dashboard/api.test.ts` (create)

**Interfaces:**
- Produces: `getDashboard(): Promise<DashboardLayoutResponse>`, `saveDashboard(layout_json, canvas_filters_json): Promise<DashboardLayoutResponse>` (mirror `lib/reports/api.ts`); `useAuth().features.customDashboard: boolean`.

- [ ] **Step 1: READ `frontend/lib/reports/api.ts`** + `AuthProvider.tsx` (how `features.reports`/`plans` are typed + consumed).
- [ ] **Step 2: Write the failing api test** asserting `getDashboard`/`saveDashboard` hit `/api/v1/dashboard` (GET/PATCH) with the auth fetch wrapper and parse the response. Mock the fetch layer like the reports api tests.
- [ ] **Step 3: Implement** `api.ts` + `types.ts` (reuse `LayoutJson`/`CanvasFilters`/`Widget` from `lib/reports/types`). Add `customDashboard` to the AuthProvider `features` type + mapping from `/auth/status`.
- [ ] **Step 4: Run test + tsc + lint.** Green.
- [ ] **Step 5: Commit.**
```bash
git add frontend/lib/dashboard/ frontend/components/auth/AuthProvider.tsx frontend/tests/lib/dashboard/
git commit -m "feat(dashboard): frontend dashboard api + customDashboard feature flag"
```

---

### Task 5: Flag-switched dashboard page (Canvas shell when on)

**Files:**
- Modify: `frontend/app/dashboard/page.tsx` (gate at the top)
- Create: `frontend/components/dashboard/CustomDashboard.tsx` (the Canvas shell)
- Test: `frontend/tests/app/custom-dashboard.test.tsx` (create)

**Interfaces:**
- Consumes: `useAuth().features.customDashboard`, `getDashboard`/`saveDashboard` (Task 4), the Reports `Canvas`/`WidgetShell`/`widgetKit`/`layout.ts`.

- [ ] **Step 1: Gate the page.** At the top of `app/dashboard/page.tsx`, branch on `features.customDashboard`: when **false**, render the existing dashboard EXACTLY as today (no behavior change — the whole current component stays the default path); when **true**, render `<CustomDashboard />`. Keep the existing dashboard code intact behind the flag.
- [ ] **Step 2: Build `CustomDashboard.tsx`** — load via `getDashboard()`, render the Reports `Canvas` + `WidgetShell` with an explicit **Customize** mode + **Save** (mirror `app/reports/[id]/page.tsx`'s load/edit/save + `gridChanged`/`widgetsFromLayout` from `lib/reports/layout.ts`); persist via `saveDashboard()`. Phase 1's layout is whatever `getDashboard` returns (the server default — a minimal widget); real finance widgets arrive Phase 2. Mobile read-only stack (reuse the `mobileStackHeight` pattern).
- [ ] **Step 3: Tests.** `custom-dashboard.test.tsx`: with the flag OFF the existing dashboard renders (assert a known current-dashboard testid); with the flag ON + a mocked `getDashboard`, the Canvas shell renders and Save calls `saveDashboard`. Mock the dashboard api + auth.
- [ ] **Step 4: tsc + lint + FULL suite.** Green. (The known `transactions-page` flake — confirm in isolation if it's the only failure.)
- [ ] **Step 5: Commit.**
```bash
git add frontend/app/dashboard/page.tsx frontend/components/dashboard/CustomDashboard.tsx frontend/tests/app/custom-dashboard.test.tsx
git commit -m "feat(dashboard): gated Canvas shell (flag on) — existing dashboard unchanged when off"
```

---

### Task 6: Verification
- [ ] Backend: `docker compose exec backend pytest tests/ -q` green; migration applies cleanly.
- [ ] Frontend: `tsc --noEmit` clean, `npm run lint` 0 errors, full suite green, design-token gate green.
- [ ] Manual: with the flag OFF, `/dashboard` is byte-identical to today. With the flag force-ON for the org (`/system/features` or an OrgSetting override), `/dashboard` shows the Canvas shell; entering Customize, moving the placeholder widget, Save, reload → layout persists; a second user is unaffected.

## Self-review (done)
- **Spec coverage:** Phase 1 of the W4 customizable-dashboard spec — persistence (model+migration T1, gate+schemas T2, endpoints T3) + the gated canvas shell (T4 api/flag, T5 page) — with the prod-safety feature gate made explicit (the spec's "placeholder default" only ever shows behind the OFF-by-default flag, so the live dashboard is never disrupted).
- **Placeholders:** boilerplate defers to named template files (Report model/router/schema, reports api, AuthProvider) rather than reproducing 200 lines; test cases + interfaces are concrete.
- **Type/name consistency:** `Feature.CUSTOM_DASHBOARD` / `custom_dashboard` (wire) / `customDashboard` (FE) used consistently; `DashboardLayout`, `getDashboard`/`saveDashboard`, `DEFAULT_DASHBOARD_LAYOUT` consistent across tasks.
- **Migration:** `068`, down_revision `067` — confirm `alembic heads` at author time (a parallel session's branch could add a migration; if head differs, rechain).
