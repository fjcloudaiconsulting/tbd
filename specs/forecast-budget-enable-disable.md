---
name: Forecast + Budget enable/disable at org level (post-critical-work)
description: Owner idea captured 2026-05-13. Move the per-page "Hide details" switch on Forecast plans (and a similar future affordance on Budget) up to Org configuration as an "Enable/Disable Forecasts" + "Enable/Disable Budgets" toggle. Not a current-batch dispatch; revisit when critical work is done.
type: project
---
# Forecast + Budget enable/disable at org level

**Captured:** 2026-05-13. Idea, not for current-batch dispatch.

## Context

Today the `/forecast-plans` page has a "Hide details" switch (client-side preference). Owner observation: this is really an org-level capability toggle in disguise — "I don't want this feature at all" rather than "I want it but collapsed today." Same shape applies to Budget.

Proposal: move both into the Org configuration surface (`/settings/organization` or a new dedicated `/settings/organization/features` tab) as proper enable/disable toggles:
- **Enable forecasts** (default ON) — when OFF, hide forecast UI entry points across the app (sidebar nav, dashboard tiles, etc.) and skip the underlying queries
- **Enable budgets** (default ON) — same shape

## Why this matters

1. UX: a user who never wants forecasts shouldn't see them surfaced repeatedly. A persisted org toggle is a more honest contract than a per-page collapse.
2. Performance: when forecasts are disabled, the dashboard skips the forecast-aggregator query path. Same for budgets.
3. Adoption signal: lets us measure "what percentage of orgs disable each surface" as a product signal once telemetry is in (L6.2).

## Open questions to settle before dispatch

- **L4.11 feature-overrides integration:** the existing `plans.features` JSON + `org_feature_overrides` table from PR #109 is the right substrate. Add keys `feature.forecasts` (default true) and `feature.budgets` (default true) to the catalog. Owner-controllable via `/system/plans` and per-org via `/admin/orgs/[id]` overrides AND via the org's own settings surface (different scope from admin override — needs threat model).
- **Migration path:** existing orgs default to ON, no migration needed for existing data. New orgs default to ON unless plan says otherwise.
- **UI removal coverage:** every entry point needs `has_feature(org_id, "feature.forecasts")` gating. Sidebar, dashboard tiles, settings tabs, navigation deep links.
- **Data preservation:** when disabled, the org's existing forecast plans / budget data MUST persist (not deleted). Re-enabling restores access.
- **"Hide details" switch fate:** keep as a per-user UI preference even after the org toggle ships? Or retire it entirely? Owner to decide.

## Dependencies / sequencing

- Best done after L4.11 plan-features infrastructure is fully wired (already shipped in PR #109). Implementation is mostly UI gating + the two new feature keys.
- Should NOT block on AWS apex / launch infrastructure work.
- Owner explicitly deferred this to "when we finish the critical work" — revisit after the current Wave (cross-org user search, rate limit overrides, reconciliation UI, apex track) settles.

## Estimated effort

S-M. ~300-500 LoC across the two feature gates, settings UI, and the entry-point audit. No new tables; no migration.

## Touch points (when dispatched)

- `backend/app/auth/feature_catalog.py` — add `feature.forecasts` + `feature.budgets`
- `backend/app/services/feature_service.py` — `has_feature` already exists
- `frontend/app/settings/organization/page.tsx` — new feature-toggle section
- `frontend/components/AppShell.tsx` — sidebar entry point gating
- `frontend/app/dashboard/page.tsx` — tile gating
- All forecast/budget routes — feature-gate or 404 when disabled
