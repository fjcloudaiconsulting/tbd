# W4 — Customizable Dashboard (+ wider canvas)

**Date:** 2026-06-23
**Status:** Approved (brainstorm complete)
**Workstream:** W4 of the 2026-06-22 product re-prioritization ([[project_reprioritization_2026_06_22]]). Follows W3 (visual charts + Sankey + mobile), shipped 2026-06-23.
**Motivation:** Operator wants a Monarch-style customizable home: rearrange/resize/add/remove dashboard widgets, including widgets cloned from saved Reports — without losing any of today's curated finance tiles. While reviewing Monarch, the operator also noted its content area is far wider than ours; ours is capped at 1280px and centered, wasting space on large monitors.

## Locked decisions (from brainstorm)

1. **Widget model:** the dashboard becomes a widget canvas containing BOTH (a) today's finance tiles, now widgetized, and (b) analytic widgets cloned from saved Reports. **Hard constraint: nothing is ever lost** — every current tile is a registered widget type; removing a tile just returns it to the picker to re-add anytime. The **default layout reproduces today's dashboard exactly**.
2. **Grid engine:** **reuse the existing Reports `Canvas`** (`react-grid-layout` 1.5) — same drag/resize, `layout_json` persistence, explicit edit-mode, mobile read-only stack. **The gridstack.js migration is explicitly DEFERRED** (it was motivated by mobile touch-editing, which is out of scope; reusing the Canvas removes the React-19 spike and migration risk entirely).
3. **Canvas width:** **widen globally — ALL routes**. Bump the single AppShell content cap from `max-w-screen-xl` (1280px) to **~1760px** (slim gutters, still centered) for every page (operator decision: apply across the board, not per-surface). Grid stays **12 columns** (existing saved Report layouts keep valid coordinates; columns just get wider).
4. **Persistence:** new per-user `DashboardLayout` model mirroring `Report`; private to the owner in v1 (no org-sharing).
5. **Finance-widget data:** a `DashboardDataProvider` context fetches shared refs once; finance widgets read from it. Analytic/report widgets self-fetch via the existing `useReportQuery`. The **period navigator stays as fixed chrome** above the canvas and drives the provider's active period.
6. **Edit model:** explicit **"Customize" mode** (drag/resize/add/remove) + **Save** + **Reset to default**, mirroring Reports. Mobile = read-only single-column stack.

## Design-system constraints that hold

- **No Off-Token Rule** — token classes only; CI-gated by `frontend/scripts/check-design-tokens.sh`.
- **The One Brass Rule / Sidebar-Always-Navy** — unchanged.
- **WCAG 2.2 AA** — widgets keep labels/legends; the W3 chart-palette + a11y work is inherited.
- **Frontend verify must include `npm run lint`** (eslint `no-explicit-any` is CI-gated and NOT covered by local `tsc`/tests) → [[reference_eslint_ci_gate_misses]].
- **No AI attribution** in commits OR PR bodies → [[feedback_no_ai_attribution]].

---

## Phase 0 — Wider canvas, app-wide (small, independent, ships first)

**Goal:** every page uses more of the monitor; no more 1280px-centered content with big empty margins.

**Mechanism.** `AppShell.tsx:620` currently hardcodes `<div className="mx-auto max-w-screen-xl">{children}</div>`. Change the cap **globally** to ~1760px for all routes: `<div className="mx-auto max-w-[1760px]">{children}</div>` (keep `mx-auto` + `p-4 sm:p-8`). One-line change, no per-route mechanism, no width variant — applies to every page (operator decision).
- Verify against the No-Off-Token gate: `max-w-[1760px]` is an arbitrary Tailwind *size* utility (not a color), so it's allowed; confirm `check-design-tokens.sh` only blocks colors and passes.
- Sanity-check a few text/form-dense pages (settings, admin, a form) at the new width — acceptable per the operator's call to widen across the board.

**Grid impact.** None to the column count — the Reports `Canvas` stays 12-col; a wider container simply makes each column wider. (Optional future: an `xl` breakpoint with more columns — explicitly out of scope; would change saved-layout semantics.)

**Acceptance:** on a >1280px monitor, all pages (`/dashboard`, `/reports`, `/settings`, admin, …) fill to ~1760px centered with slim gutters; no horizontal scroll at any width; `check-design-tokens` + `tsc` + lint + full suite green.

---

## Phase 1 — Dashboard persistence + canvas shell

**Goal:** the dashboard renders through the Reports Canvas from a persisted per-user layout; save/load/edit works. (Tiles NOT yet widgetized — this phase proves the shell with a placeholder/default layout.)

### Backend
New model `DashboardLayout` (`backend/app/models/dashboard.py`), mirroring `Report`:
`id, owner_user_id (FK users, indexed), org_id (FK orgs), layout_json (JSON), canvas_filters_json (JSON), schema_version (int), created_at, updated_at`. Unique on `owner_user_id` (one layout per user in v1). Alembic migration.

Endpoints (`backend/app/routers/dashboard.py`, `APIRouter(prefix="/api/v1/dashboard")`, `get_current_user`, no feature gate — dashboard is core):
- `GET /api/v1/dashboard` → returns the caller's layout; **auto-creates the default layout** (server-rendered `DEFAULT_DASHBOARD_LAYOUT`) on first access. Org+user scoped.
- `PATCH /api/v1/dashboard` → owner-only; validates `layout_json`/`canvas_filters_json` with the SAME strict Pydantic validators Reports uses (reuse `schemas/report.py` layout validation — extract a shared validator if needed). Per the PR #424 lesson: validate as a side-effect and return the blob VERBATIM — never `model_dump`-round-trip it, or real widget knobs get silently stripped.
- Pydantic schemas in `backend/app/schemas/dashboard.py` reusing `LayoutJson`/`CanvasFilters` shapes.

### Frontend
- `frontend/lib/dashboard/api.ts`: `getDashboard()`, `saveDashboard(layout_json, canvas_filters_json)` (mirror `lib/reports/api.ts`).
- The dashboard page renders the existing `Canvas` + `WidgetShell` + explicit Customize/Save, loading from `getDashboard()`. For this phase the default layout may contain a single placeholder/KPI widget to prove the round-trip; the real tiles arrive in Phase 2.
- Reuse `layout.ts` (`widgetsFromLayout`, `gridChanged`) and the edit/save pattern from `app/reports/[id]/page.tsx`.

**Acceptance:** a user can load `/dashboard` (auto-created layout), enter Customize, drag/resize a widget, Save, reload → layout persists; another user is unaffected (per-user scoping test); backend tests cover auto-create, owner-only PATCH, validation rejection; `tsc`+lint+suites green.

---

## Phase 2 — Widgetize the finance tiles

**Goal:** today's tiles become first-class dashboard widgets; default layout = today's dashboard exactly; tiles are removable AND re-addable (the hard constraint).

### Widget catalog (new dashboard widget types)
Extract from `app/dashboard/page.tsx` (1,327 lines) + `components/dashboard/*` into widget components, each with a config type, an `emptyX` factory, a `renderDashboardWidgetByType` case, and a picker entry:
| Widget type | Source today | Notes |
|---|---|---|
| `dash_on_track` | `OnTrackTile` | forecast verdict hero (full-width default) |
| `dash_accounts` | `AccountTile` list | accounts + balances |
| `dash_account_forecast` | `AccountMonthEndForecast` | per-account month-end |
| `dash_spending_donut` | Spending-by-category donut | reuse W3 donut styling |
| `dash_budget_progress` | Budget bars | |
| `dash_forecast_category` | Forecast-by-category bars | |
| `dash_recent_transactions` | Recent-transactions table | paginated |

These live in `components/dashboard/widgets/` and register in a dashboard widget kit (`lib/dashboard/widgetKit.tsx`) that composes with the Reports `renderWidgetByType` (so a dashboard can render BOTH dashboard-finance widgets and analytic report widgets). The `WidgetType` union gains the `dash_*` members (in a dashboard-scoped type module to avoid bloating reports types; the `Canvas`/`WidgetShell` only depend on `{id,type,title,grid}` so they stay agnostic).

### Data: `DashboardDataProvider`
A context provider (wrapping the canvas) fetches the shared refs ONCE — accounts, billing period(s), budgets, forecast projection, account-balance forecast, recent transactions — replicating today's `loadRefs`/`loadTransactions`/`loadForecast*`. Finance widgets consume the context (no per-widget refetch). Post-write refresh (`refreshAllPostWrite`) lives in the provider. The **period navigator** is fixed chrome above the canvas; it sets the provider's active period; finance widgets react.

### Default layout
`DEFAULT_DASHBOARD_LAYOUT` reproduces today's arrangement (on-track hero full-width row 1; accounts + account-forecast row 2; 3 category charts row 3; recent transactions row 4) using the new `dash_*` widgets at the corresponding grid coords. New users and "Reset to default" get this. Existing users (Phase 1 auto-created a placeholder) get a one-time migration/normalization to this default, OR Phase 1's auto-create is updated to emit this default (sequence so no user is stranded with the placeholder — Phase 2 ships the real default before any real users customize).

### Picker
Extend `WidgetPicker` with a **"Dashboard"** group listing the 7 `dash_*` types (so a removed tile is always re-addable — satisfies the hard constraint). Finance widgets may be added more than once (harmless; e.g., two account tiles with different scope later).

**Acceptance:** `/dashboard` renders identically to the pre-W4 dashboard via the default layout; user can remove any tile and re-add it from the picker; drag/resize/save persists; mobile read-only stack renders each finance widget at a usable height; backend/frontend suites + lint + token gate green; the old monolithic page logic is fully migrated (no dead duplicate fetch paths).

---

## Phase 3 — Add-from-report + reset + mobile polish

**Goal:** clone analytic widgets from saved Reports onto the dashboard; finish reset + mobile.

- **Add from report:** the picker gains a **"From a report"** path → list the user's saved reports (`GET /api/v1/reports`) → list the chosen report's widgets (from its `layout_json`) → **clone the selected widget's config** (deep copy, fresh `id` + grid position) into the dashboard layout. Cloned analytic widgets self-fetch via `useReportQuery` (already report-agnostic — it only needs the widget config). No reference/linkage kept (copy stays independent, offline-safe).
- **Reset to default:** a Customize-mode action that restores `DEFAULT_DASHBOARD_LAYOUT` (confirm modal).
- **Mobile read-only pass:** reuse W3 PR3's `mobileStackHeight` pattern, extended to the `dash_*` widget types (each gets a sensible stack height); finance widgets render read-only in the single-column stack; the period navigator stays usable.

**Acceptance:** user clones a chart from a report and it renders + queries correctly on the dashboard; reset restores the default; on 360/390px the dashboard is a legible read-only stack with no horizontal scroll; suites + lint + token gate green.

---

## Reuse map (what we are NOT rebuilding)
- `components/reports/Canvas.tsx`, `WidgetShell.tsx`, `WidgetPicker.tsx` (extended, not forked), `lib/reports/layout.ts`, the `Widget`/`LayoutJson`/`WidgetGrid` types, and the load/edit/save pattern from `app/reports/[id]/page.tsx`.
- `useReportQuery` for cloned analytic widgets (unchanged).
- W3 chart widgets + the 8-hue palette (the donut/bars on the dashboard inherit the W3 visual refresh).

## Out of scope (explicit)
- gridstack.js migration / mobile touch-editing (deferred; separate future effort).
- Org-shared dashboards (v1 is per-user private).
- More grid columns / an `xl` breakpoint (keeps saved-layout coords stable).
- Plans on the canvas (future; Phase 0 width benefits it when it comes).
- Multiple saved dashboards per user (one layout per user in v1).

## Sequencing & review
Phase 0 → 1 → 2 → 3, each its own PR (Phase 0 independent; 2 depends on 1; 3 depends on 2). Each PR via subagent-driven-development + per-task review + a whole-branch review, with `npm run lint` in the verify set. Phase 0 can ship immediately (also improves Reports).
