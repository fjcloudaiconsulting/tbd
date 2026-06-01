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

## Architect review — required revisions (2026-06-01)

Review verdict was "needs revision." `org_settings` is confirmed KEY/VALUE (`backend/app/models/settings.py:9-23`), generic `GET/PUT /api/v1/settings` (admin-only, `routers/settings.py:42-116`) already used by `frontend/app/settings/organization/page.tsx` — so **no migration**, and add a `get_org_setting(db, org_id, key, default)` accessor (mirror the inline pattern at `import_service.py:436`). The following are now binding:

**R1 (was B1) — make the master-only validation mode-aware on BOTH write paths.** `_validate_master_category` (`forecast_plan_service.py:84-108`) raises `ValidationError("...must use master categories...")` whenever `cat.parent_id is not None` (line 102-103) and is called by `upsert_item` (515-517); `bulk_upsert` has the SAME rejection inlined (570-575). This is the core blocker — sub items are rejected before any guard runs. Thread `forecast_input_granularity` into both: in `subcategory` mode allow `parent_id is not None` (reject masters per the guard); in `master` mode unchanged.

**R2 (was B2) — define and enforce auto-populate/copy behavior under subcategory mode.** `populate_from_sources` / `refresh_from_sources` roll every recurring/history txn up to its master (`319-321, 334, 356-364, 408, 461-468`) and `copy_from_period` copies `category_id` verbatim (738-759); all write via `db.add` + bulk `commit` (475), **bypassing `upsert_item` and its validation/guard**. Decision: **populate becomes granularity-aware** — in `subcategory` mode it groups history/recurring by the transaction's own subcategory (not rolled to master); in `master` mode it rolls to master (current). The per-master guard must be enforced **inside the populate/copy paths too** (not only `upsert_item`): populate must emit guard-consistent items and must not create a master item for a master that already has manual sub items (or vice versa) — skip such masters. This closes the double-count hole (master item + sub items both summed in `_build_response:239-243`).

**R3 (was B3) — enforce the guard in `bulk_upsert`.** `bulk_upsert` (548-613) has no sibling/per-master awareness and commits directly. The per-master XOR guard (a master built by one master item OR its subs, never both) must be applied in `upsert_item`, `bulk_upsert`, AND the populate/copy paths. Reject mixing with a clear code (`mixed_granularity`, 409/422). Put the guard in one shared helper called by all write paths.

**R4 (was N2) — correct the actuals semantics.** `_compute_actuals_batch` keys actuals by the item's `category_id` (141, 187, 223); for a sub item the children query (144-148) returns nothing, so a sub item's actual = transactions tagged to **that exact subcategory**, not the master. So in subcategory mode actuals are per-sub (summing subs reproduces the master). This is correct ONLY if the org's transactions are categorized at sub level (true for this owner, who is recategorizing). The earlier "actuals already aggregate to master" wording is corrected: in sub mode, actuals aggregate to **sub**, and the master comparison = sum of its subs.

**R5 (was N1) — UI display scope.** The planned list already renders one row per item and totals sum across items (`ForecastPlansClient.tsx:1207-1339`, `forecast_plan_service.py:239-243`), so **no master roll-up is needed for totals to be correct**. Sub items render as their own rows under their subcategory name, and the chart (597-606) plots per-item (sub-level bars). **In scope:** sub items as their own rows; totals correct. **Out of scope (v1):** visual master-grouping of sub rows / a rolled-up master bar — flagged; add later if the owner wants the list/chart grouped by master.

**R6 (minor) — frontend.** `resolveMasterId` (`ForecastPlansClient.tsx:472`) becomes conditional on mode (skip in sub mode). The disable predicate replacement is implementable from `plan.items` + `categories` (+ `CategorySelect` already supports `disabledIds` with the "(already added)" treatment). After a mode switch, existing master items stay valid and the predicate disables adding subs under them until the master item is removed — coherent; surface a short reason in the disabled hint.
