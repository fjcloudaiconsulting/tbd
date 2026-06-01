# Forecast plan: subcategory-level items + per-org build-granularity preference

**Status:** draft for review, 2026-06-01.
**Source:** product owner, recategorizing in prod. When adding a forecast item manually, selecting a subcategory disables the master and all its sibling subcategories, so you can't add more than one subcategory under a master. Desired: build a master's forecast from multiple subcategory items that sum into the master, configurable per org.

## Verdict (current behavior — from investigation)

- Forecast items store only the **master** category id: the frontend resolves a selected subcategory up to its master before saving (`frontend/app/forecast-plans/ForecastPlansClient.tsx` ~line 472).
- Unique constraint `uq_forecast_item_plan_cat_type` on `(plan_id, category_id, type)` (`backend/app/models/forecast_plan.py:60-62`) = one row per master per type.
- The disable logic (`ForecastPlansClient.tsx:338-350`) marks a master used, then **also disables every child** of a used master, so after one subcategory the whole master is locked.
- Actuals already roll up to the master in `_compute_actuals_batch` (`backend/app/services/forecast_plan_service.py:127-191`).

## Goal

Let a master's planned forecast be built from **multiple subcategory items** (each summing into the master total), with a **per-org preference** for the default build granularity. The forecast is always reported at the master level.

## Design

### Per-org preference
- New org-level setting `forecast_input_granularity` with values `master` | `subcategory`, **default `master`** (preserves today's behavior for everyone else).
- Storage: confirm how `org_settings` is modeled (typed columns vs key/value) by reading `backend/app/models/org_settings.py` (or equivalent) + how `share_merchant_data` is stored. If it's typed columns, add a column via a small migration; if key/value, no migration. Expose via the org settings read/update path the frontend already uses.
- UI: a "Build forecasts by: Master categories | Subcategories" control. Place it on the forecast plan page (most discoverable) and/or Settings -> Organization; it persists to the org setting. Org-scoped, so all members build consistently.

### Item granularity
- **`master` mode** (default): unchanged. The add picker offers masters; the item stores the master id.
- **`subcategory` mode**: the add picker offers subcategories; the item stores the **subcategory** id (stop resolving to master). Multiple subs of one master coexist under the existing `(plan, category, type)` unique constraint (different category ids). No item migration needed.

### Per-master integrity guard (ALWAYS on, both modes) — prevents double-counting
For a given `(plan, master, type)`, the forecast is built **either** by one master-level item **or** by one-or-more subcategory items of that master, **never both**.
- Backend (in `upsert_item` / create path): reject adding a master-level item when subcategory items for that master already exist (and vice versa) with a clear 409/422 code (e.g. `mixed_granularity`).
- Frontend disable predicate (replace `ForecastPlansClient.tsx:338-350`): disable only the **exact** category already added; additionally, if a master has subcategory items, disable the master option; if a master has a master-level item, disable its subcategories. (No longer "disable all children whenever the master is used.")

### Roll-up / display
- A master's **planned** total = its master-level item amount, OR the sum of its subcategory items' amounts.
- The forecast comparison/display stays at the master level (actuals already aggregate to master). Verify the planned-amount rendering sums subcategory items into the master row; add a master roll-up where the UI currently assumes one row per master.

## Migration
- Items: none (`category_id` already FKs `categories`, which includes subcategories).
- Org setting: only if `org_settings` uses typed columns (then one nullable column, default `master`).

## Tests
- Backend: in subcategory mode, two sub items under one master both persist and the master's planned total = their sum; the mixed-granularity guard rejects master+sub coexistence (both directions); master mode unchanged.
- Frontend: after adding one subcategory, **other subcategories of the same master remain enabled** (the core bug), only the exact added one is disabled, and the master option is disabled; in master mode behavior is unchanged; the granularity control flips the picker.

## Out of scope
- Changing the actuals roll-up (already master-level).
- Retroactively converting existing master-level items into subcategory items on mode switch (existing items remain valid; the guard governs new adds).
- Per-user (vs per-org) preference — owner chose per-org.

## Delivery
- Branch `feat/forecast-subcategory-items` off `main` (this worktree), own PR. Subagent-driven, TDD; backend tests in an isolated compose project so they never touch the user's dev MySQL. No em-dashes; no AI attribution. Prod-targeted (owner is recategorizing now).
- Local build note: cleanest once reports PR #382 merges (then `main` matches the dev DB's migration head); otherwise built/verified in an isolated stack.
