# Reports — reportable-transaction filter + "Include transfers & adjustments" toggle

**Date:** 2026-07-16 · **Status:** approved (in-conversation), implementing

## Problem

`reports_query_service.compile_ast_to_query` filters transaction-source report
widgets (bar/pie/line/table) only by `org_id` + the user's explicit filters. It
does **not** apply `reportable_transaction_filter()`. So Reports silently count:

- transfer legs (`linked_transaction_id IS NOT NULL`) — not income/expense
- manual balance adjustments (`is_manual_adjustment = True`)
- skipped/rejected reconciliation rows (`reconciliation_state IN (skipped, rejected)`)

Budgets, Forecast, and the Sankey widget all apply the filter. Reports are the
outlier. Logged at Reports v2 spec time (2026-05-22) as "a gap, not a bug", so
this is a deliberate product decision, not an obvious fix — and it changes the
numbers in existing saved reports.

## Decision

**Default = exclude** (apply `reportable_transaction_filter()`), matching every
other aggregate surface. Add an opt-in **"Include transfers & adjustments"**
per-widget toggle for power users who want raw activity.

**What the toggle re-includes** (the one real design call):

| Excluded row type | Re-included by toggle? | Why |
|---|---|---|
| Transfer legs | ✅ | Legit "show cash movement" use case |
| Manual balance adjustments | ✅ | Users may want to see corrections |
| Skipped / rejected recon rows | ❌ **never** | Their amount was reverted from `accounts.balance`; counting them double-counts a balance that no longer includes them. Audit-only. |

So even with the toggle ON, reverted recon rows stay excluded.

## Design

**Backend**
- `schemas/reports_query.py`: add `include_non_reportable: bool = False` to the
  `ReportsQuery` AST (whitelisted under `extra="forbid"`).
- `services/transaction_filters.py`: add `non_reverted_transaction_filter()` =
  `reconciliation_state NOTIN (skipped, rejected)` (the always-on part).
- `services/reports_query_service.compile_ast_to_query`: after the `org_id`
  WHERE, apply `reportable_transaction_filter()` when the flag is off, else
  `non_reverted_transaction_filter()`. Transactions-source-only by construction
  (the compiler builds `select_from(Transaction)`; accounts/recurring sources
  have separate compilers and ignore the flag).

**Frontend**
- `lib/reports/types.ts`: `WidgetFilters.include_non_reportable?: boolean`.
- `lib/reports/resolve.ts`: set the AST's `include_non_reportable` from the
  widget filter (top-level AST field, not a `filters[]` entry).
- Widget Editor → Filters tab: an "Include transfers & adjustments" checkbox +
  helper text, gated to transaction-source widgets (like Status/Amount).

**Back-compat:** none needed (pre-launch). Old reports have no flag → default
exclude. Dashboard tiles inherit the corrected default (no toggle UI there).

**Scope:** does not touch accounts/recurring sources, Sankey (already excludes),
Budgets, or Forecast.

## Tests
- Backend: default excludes all three row types; flag re-includes transfer legs
  + manual adjustments; reverted recon rows never returned; non-transactions
  sources unaffected; unit test for `non_reverted_transaction_filter()`.
- Frontend: toggle renders only for transaction-source widgets; resolves into
  the AST's `include_non_reportable`.
