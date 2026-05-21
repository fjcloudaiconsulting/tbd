---
name: Category Fallback Design (post-L3.10) — PARTIAL
description: Architect-approved 4-layer design. Layer A SHIPPED via PR #108 (empty-state UX on import). Layers B (type-specific category requirement at preview time + structured 400) and C (Restore recommended categories action) still PENDING. Layer D (slug aliasing) deferred to P-IL post-launch.
type: project
---
**STATUS: 🟡 PARTIAL.**
- **Layer A** — empty-state UX on import. ✅ SHIPPED via PR #108 (`feat/import-empty-categories-state`, 2026-05-02).
- **Layer B** — type-specific category requirement at preview time + structured 400 contract. 🔵 PENDING. Needs backend change in `import_service.build_preview` + structured error + frontend handling.
- **Layer C** — "Restore recommended categories" button (re-runs `SYSTEM_CATEGORIES` seed for current org, idempotent). 🔵 PENDING. Service + UI + tests.
- **Layer D** — Category slug aliasing for shared-dictionary lookups. ⏳ DEFERRED to post-launch P-IL track.

## Problem

L3.10 import preview silently dead-ends if the user has zero categories. The UI's "Confirm Import" button stays disabled (because `default_category_id` is required and the dropdown is empty), with no error message. Same dead-end can hit transaction-create flows. Discovered when the user removed all pre-seed system categories from their prod account on 2026-05-02.

## Architect's recommended shape (2026-05-02)

Four-layer design, increasing scope:

### A. Empty-state UX when zero categories — SHIP NOW

If `categories.length === 0` on the import page, render a clear empty state with a CTA button/link to `/categories` for category creation. Replaces the silent dead-end with an actionable message. Frontend-only, ~15 min.

**Status:** PR #108 opened 2026-05-02 (`feat/import-empty-categories-state`). Empty state shown when SWR has loaded an empty array; upload + preview steps gated off so the user can't reach the disabled confirm. Loading state unchanged. Backend / smart-rule suggestions / `default_category_id` contract untouched. 53/53 frontend tests pass.

### B. Type-specific category requirement at preview time — REFINED, NOT YET SHIPPED

I originally proposed "require at least one income + one expense category before allowing import." Architect pushed back: that's too broad. **An expense-only CSV shouldn't be blocked because the user has no income category.**

**Refined design:** at preview time, the backend has already parsed the rows and knows which `type` values are present. Inspect the parsed row set and require at least one compatible category for each type actually present:

- if any rows have `type == "expense"` → require `count(categories WHERE type IN ("expense", "both")) >= 1`
- if any rows have `type == "income"` → require `count(categories WHERE type IN ("income", "both")) >= 1`

If a required category type is missing, return a 400 with a structured `detail` payload the frontend can surface as a targeted message ("This CSV has expense rows but you have no expense category. Add one to continue.").

This belongs in `import_service.build_preview` or a new pre-flight check. **Not Saturday-sized:** needs a backend change + structured error contract + frontend handling. Capture as a follow-up.

### C. "Restore recommended categories" button — LONG-TERM RECOVERY PATH

A button on `/categories` (or `/settings/organization`) that re-runs the `SYSTEM_CATEGORIES` seed for the current org. Idempotent: skip slugs that already exist. Two reasons users want this:

1. They deleted system categories by accident or to clean up, then realized they want them back.
2. They started with the "skip pre-seed" option in onboarding (when L3.3 ships) and later decide to opt in.

Pairs naturally with L3.3 onboarding. Treats pre-seed as a deliberate choice (which fits the user's "optional pre-seed + tour" vision). **Not Saturday-sized:** ~1 day for service + UI + tests.

### D. Category slug aliasing — ARCHITECTURAL, BELONGS IN P-IL

L3.10's shared `merchant_dictionary` resolves slugs against `categories.slug` AND `categories.is_system=True`. A user with only custom categories ("Food" instead of "Groceries") gets ZERO shared-dictionary suggestions.

**The fix:** add an `alias_slug` field on Category so a user-created "Food" can claim slug `groceries` and pick up shared-dictionary suggestions. Lookup chain becomes:

```
WHERE org_id=? AND (
    (slug=? AND is_system=True) OR
    (alias_slug=?)
)
```

This is real architectural work tied to the P-IL (Localized Import Intelligence) thread and overlaps with P-IL.2 (per-locale `merchant_dictionary` extension). Captured in `project_localized_import_intelligence.md` as part of P-IL.

## Why this came up now

The user removed all pre-seed system categories from their prod account before testing the L3.10 ING NL CSV import. The preview returned zero suggestions (Tier-2 dictionary lookup needs system-categories with matching slugs) AND the confirm dropdown was empty (no categories to pick as default). They hit the silent dead-end.

## Sequence

1. **Saturday 2026-05-02:** Ship A. Open small PR: empty state on `/import` when `categories.length === 0`.
2. **Next-iteration backlog:** B-refined as a small backend + frontend pair. Roughly half-day.
3. **L3.3 onboarding:** ship C as the "I want pre-seed back" recovery button.
4. **P-IL thread:** D as part of the locale-aware dictionary work.
