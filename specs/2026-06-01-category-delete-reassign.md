# Category delete-with-reassign (single-delete UX) + migrate rules

**Status:** draft for review, 2026-06-01.
**Source:** product owner is recategorizing all expenses in prod and "category removal is becoming a problem." Investigation verdict below.

## Verdict (current behavior)

The backend already supports delete-with-migration: `delete_category_with_migration` (`backend/app/services/category_service.py:1126`) requires a `target_category_id` when the category has dependents and bulk-moves them, then deletes the source. The pain is:

1. **The frontend single-delete path** (`frontend/app/categories/page.tsx:281`) calls `DELETE` with **no target**, so deleting any category that has transactions returns a bare **422 `migration_target_required`** with no inline way to choose a target — a dead end. (The batch-delete modal does it correctly.)
2. **Data-loss footgun:** on delete-with-migration the source category's **`category_rules` are deleted, not migrated** (`category_service.py:1259-1261`). Removing a category silently drops its learned auto-categorization, which can re-introduce miscategorization during a recategorization.

## Scope (Option 1 — owner-selected)

Fix the single-delete UX to reassign, and preserve auto-categorization rules. **In scope:**

### Backend — migrate `category_rules` on delete-with-migration
- In `delete_category_with_migration`, when a `target_category_id` is supplied, **re-point** the source category's `category_rules` to the target instead of deleting them.
- **Uniqueness handling:** `category_rules` is unique per org on its normalized match key (verify the exact constraint in `backend/app/models/category_rule.py` + its migration). When re-pointing would collide with a rule the target already has for the same key, **keep the target's existing rule and drop the source's** (the target is the surviving category). Do this in the same transaction as the existing transaction/recurring/forecast migration.
- Everything else in the migration path stays as-is. **Budgets remain deleted** on category removal for now (out of scope — see below); add a one-line code comment noting it's intentional pending a separate decision.
- Tests: deleting a category with rules + a target migrates the rules (and dedupes against an existing target rule); confirm transactions/recurring/forecast still migrate; confirm the audit event still fires.

### Frontend — inline reassign on single delete
- On the categories page, when the user deletes a category, **detect whether it has dependents** (transactions/recurring/forecast). If it does, show an **inline migration-target picker** (reuse the picker + type-compatibility filtering already built in `frontend/components/categories/BatchDeleteModal.tsx`) before issuing the delete, and pass `target_category_id`.
- If the category has **no dependents**, delete directly (no picker) as today.
- Surface the existing 409 guard errors clearly (don't regress): `has_children` (delete the subcategories first), `last_in_type` (floor invariant), `type_mismatch` (pick a same-type target). Reuse the human-readable reason mapping the batch modal already has.
- Tests: deleting a category with transactions opens the picker, choosing a target issues `DELETE` with `target_category_id` and succeeds; a no-dependent category deletes without a picker; a `has_children`/`last_in_type` error shows the right message.

## Out of scope (deliberate — possible follow-ups)

- **Merge endpoint** (`POST /categories/{id}/merge`) and **bulk transaction reassignment** on the transactions list — owner chose the minimal scope; these are the natural next phases if removal is still slow.
- **Master cascade delete** (deleting a master with its children in one go) — still requires deleting/moving children first; unchanged.
- **Budget migration** on category removal — budgets are still deleted on delete-with-migration. Flagged because it's also silent data loss, but merging per-period budget rows across categories is ambiguous (sum? pick one?); deferred to its own decision.
- The categories tree pagination/standardization (separate Tables workstream) — unrelated.

## Delivery

- Branch `feat/category-delete-reassign` off `main` (this worktree), separate PR from the reports PR #382.
- No schema migration required (logic-only backend change + frontend).
- Subagent-driven, TDD; backend tests run in an **isolated compose project** (`-p team-category-*`) so they never touch the user's dev MySQL volume.
- No em-dashes in customer-facing copy; no AI attribution in commits/PR.
- This is a prod-targeted fix; it deploys after merge (the owner is recategorizing in prod now).
