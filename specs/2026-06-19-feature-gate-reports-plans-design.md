# Feature Gate (Reports + Plans) + Reports Usefulness Pass

**Date:** 2026-06-19
**Status:** Approved, in implementation
**Author:** brainstorm session (operator: flamarion@fjconsulting.io)

## Problem

Reports and Plans are both shipped but weak. Plans is a what-if life-event
simulator with an open backlog to rethink it wholesale — currently low value.
Reports has a flexible canvas but (a) can't easily answer the money questions
users actually have, and (b) carries real bugs. The operator wants to be able
to **gate both features globally and override per organization**, then make
Reports genuinely useful while deferring the Plans rethink.

Confirmed direction (brainstorm): **gate both, fix Reports, defer Plans.**

## Goals

1. A reusable feature gate with a global on/off plus a per-org override.
2. Disable Plans globally now; keep operator access via an org override.
3. Fix three real Reports bugs and lower the barrier to common questions via
   starter templates.

## Non-Goals

- The Plans rethink/redesign (stays on its existing backlog).
- New Reports data-model capability (e.g. first-class per-account
  settled-balance vs pending-balance). Settled-vs-pending is derived from
  transaction `status`, which is sufficient.
- Self-serve org-admin feature enablement (see Decision 1).
- Server-stored report templates (see Decision 2).

## Decisions

- **Decision 1 — org override is superadmin-only.** The per-org override is set
  by the platform superadmin from the Organizations admin, NOT self-serve by an
  org's own admin. A globally-disabled feature must not be re-enableable by the
  org itself, or the gate is meaningless.
- **Decision 2 — templates are client-side config builders.** "New report from
  template" pre-fills the existing report editor with a working widget config.
  No new backend table, no server-stored templates. Templates are living
  examples the user edits and then saves as a normal report.
- **Decision 3 — org override is tri-state** (inherit / force-on / force-off),
  where "inherit" == no `OrgSetting` row.

## Architecture

### Feature gate resolution (three levels)

Evaluated per request for the caller's org:

```
effective(feature, org):
  1. org override     OrgSetting key "feature.<name>" in {on, off}     -> wins if present
  2. global default   system_settings key "feature.<name>" in {on,off} -> wins if present
  3. env-var floor     FEATURE_REPORTS_V2 / FEATURE_PLANS code default  -> fallback
```

Gateable features are an enum in code: `reports`, `plans` (extensible). With no
DB rows written, behavior is identical to today's env-var-only gating, so this
is backward compatible and needs no data migration beyond the new table.

### Data model

- **New `system_settings` table** — singleton/global key/value, superadmin
  scope. Mirrors the existing `OrgSetting` shape (`key` unique, `value` text).
  Keys used: `feature.reports`, `feature.plans` with value `on` | `off`.
- **Org override reuses existing `OrgSetting`** — keys `feature.reports`,
  `feature.plans`, value `on` | `off`. Absent row = inherit.
- Alembic migration adds `system_settings` only.

### Backend surfaces

- `app/services/feature_gate.py` (or similar): `resolve_feature(name, org, db)`
  helper implementing the three-level resolution; an enum of gated features;
  a small read-through that loads the org override + global rows.
- Gated routers depend on a `require_feature("reports"|"plans")` dependency
  that raises `404` when off. Reports reuses/replaces today's
  `require_reports_v2_enabled`. The Plans/scenarios router gains the same guard.
- `/api/v1/auth/status` returns resolved `features: { reports: bool, plans:
  bool }` for the caller's org (replaces the single `feature_reports_v2`).
- Superadmin endpoints to (a) read/write global `system_settings` feature flags
  and (b) read/write a given org's override. Every write emits an
  `audit_events` row.

### Frontend surfaces

- `AuthProvider` / `useAuth()` exposes `features.reports` and `features.plans`
  (replaces `featureReportsV2`).
- `AppShell` hides the Reports and Plans nav items when their resolved flag is
  off; the routes render a 404/empty state when off.
- **Superadmin global toggle:** a "Feature flags" card in `/system` — on/off per
  feature, writes `system_settings`.
- **Superadmin per-org override:** on the platform Organizations admin, a per-org
  control with inherit / force-on / force-off.
- **Quick-add button:** add `/reports` and `/plans` to the `SHOW_ON` allowlist
  in `shouldShowAddTransactionCta`.

### Plans disable

- Add `FEATURE_PLANS` env floor defaulting **off**. Plans nav + `/plans` routes
  + the scenarios API gate off globally. Operator keeps access via a force-on
  org override on their own org.

### Reports bugfixes

- **Breakdown tooltip bug** (primary Category Group + breakdown Category shows
  unrelated categories): repro first (regression test). Expected behavior — a
  hovered bar's tooltip shows only series that have data for that group, not
  every globally-distinct secondary value backfilled to 0. Root cause is in the
  pivot (`frontend/lib/reports/series.ts` `pivotBySecondaryDimension`) and/or
  the bar widget tooltip (`frontend/components/reports/widgets/BarWidget.tsx`).
- **Table per-column value source:** the editor currently can't change the
  measure of an added column. Repro, then allow each table column to
  independently pick its measure/agg within the source
  (`TableWidget.tsx` + the widget editor popover).
- **Quick-add button:** allowlist addition (above).

### Reports templates + discoverability

- "New report from template" entry that instantiates a **new, fully-editable**
  report with a pre-built widget config. Starter set:
  - **Net account position** — sum balances over selected accounts
    (AccountsSource `sum_balance`).
  - **Settled vs pending** — TransactionsSource `status` dimension + `sum_amount`.
  - **Balances by account** — AccountsSource, account dimension + `sum_balance`.
  - **Spending by category** — TransactionsSource, category dimension +
    `sum_amount`.
- Clearer measure/dimension labels and an empty-state that points to templates.

## Phasing (3 PRs)

1. **Gate foundation** — `system_settings` table + migration + resolution helper
   + `status` endpoint change + Plans disabled + both superadmin surfaces +
   audit writes. Self-contained; ships the gate.
2. **Reports bugfixes** — breakdown tooltip, table per-column source, quick-add.
   Each repro'd via TDD.
3. **Reports templates + labels/empty-state.**

## Testing

- **Backend:** resolution-matrix unit tests (all three levels × org-override
  permutations), gate-404 tests for reports + plans, audit-write tests for each
  toggle, `/auth/status` resolved-features test.
- **Frontend:** nav/route gating per flag, breakdown-pivot regression, table
  per-column measure selection, template instantiation, quick-add visibility.
- Run full `vitest run` + `eslint . --quiet` + `tsc --noEmit` locally before
  each PR (CI gates on all three).
- Backend tests in an isolated compose project (`-p team-<name>`) per CLAUDE.md.

## Backward Compatibility / Rollout

- No data migration of existing behavior: env-var floors preserve current prod
  state until a DB row is written.
- `.do/app.yaml`: add `FEATURE_PLANS` (default off). `FEATURE_REPORTS_V2`
  remains the reports floor.
- Frontend rename `featureReportsV2` -> `features.reports` is internal.
