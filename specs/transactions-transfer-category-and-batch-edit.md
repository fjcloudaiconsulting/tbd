# Transactions page: transfer-category consistency, add-button cleanup, batch edit

**Date:** 2026-06-05
**Status:** Spec — ready for implementation plan
**Branch target:** three independent feature branches off `main` (three PRs)

## Overview

Three independent fixes on the Transactions page, shipped as three separate PRs:

1. **PR 1 (`fix`)** — Transfer category consistency: stop letting a transfer be
   recategorized to a non-`both` category.
2. **PR 2 (`feat`)** — Remove the redundant in-page "+ New Transaction" button +
   inline form; port its "Make recurring" capability into the shared header
   quick-add.
3. **PR 3 (`feat`)** — Batch edit: multi-select + edit (category / status /
   account / tags), mirroring the existing batch delete.

**Ship order:** PR 1 → PR 3 (PR 3 reuses PR 1's guard). PR 2 is independent.

**Data-cleanup note:** PR 1 validates the *target* category, not the existing
one. The "Transfer" category is type `both`, so recategorizing an existing
mis-categorized transfer *to* "Transfer" is always allowed, before or after
PR 1. PR 1 does not freeze or migrate existing rows; mis-categorized transfers
keep being (correctly) excluded from Forecast/Budget until manually fixed. Once
PR 3 lands, the owner can fix them all in one batch operation.

---

## PR 1 — Transfer category consistency (`fix`)

### Problem

A transfer is two linked `Transaction` rows (one expense leg, one income leg)
sharing one category, bound by `linked_transaction_id`
(`backend/app/models/transaction.py`). Transfers are deliberately excluded from
all income/expense aggregates via `reportable_transaction_filter()`
(`backend/app/services/transaction_filters.py:37-51`, the
`linked_transaction_id IS NULL` clause) — moving money between your own accounts
is not spending. This exclusion is **correct and stays**.

Transfer **creation** enforces "category must be `CategoryType.BOTH`":

- Frontend picker passes `typeFilter="BOTH"`
  (`frontend/components/ui/CategorySelect.tsx`).
- Backend `validate_transfer_category()`
  (`backend/app/services/transaction_service.py:187-211`) rejects any non-`both`
  category.

But every transfer **edit** path skips that rule:

- The edit row passes `filterType={editType}` (expense/income), not `"BOTH"`
  (`frontend/app/transactions/page.tsx` edit picker).
- Backend `update_transaction()`
  (`backend/app/services/transaction_service.py:443-691`) only calls
  `validate_category_for_type()` (line ~537), which checks expense/income
  compatibility — never the transfer `both` rule.

Result: a transfer can be recategorized to an expense/income-only category
(e.g. "Credit Cards" under master "Debit & Repayments"). It then silently
vanishes from Forecast/Budget (still linked → still excluded), reading as a bug
and inviting double-count misinterpretation (the underlying CC charge was
already categorized when it was created).

### Goal

Make every category-write path obey the same rule creation already enforces:
**a transfer leg (`linked_transaction_id IS NOT NULL`) may only carry a
`CategoryType.BOTH` category.** Recategorizing among `both` categories stays
allowed (so the owner can set them to "Transfer").

### Backend — central guard

Add one rule at the service layer and route all category writes through it:

> If the transaction being written is a transfer leg
> (`linked_transaction_id IS NOT NULL`) and the new `category_id` resolves to a
> category whose `type != CategoryType.BOTH`, raise `ValidationError`
> ("Transfer category must accept both income and expense") → HTTP 400.

Apply in / verify coverage for the category writers enumerated below:

| Path | File | Action |
|------|------|--------|
| Single update | `transaction_service.update_transaction` (~line 537/565) | Add transfer-leg `both` guard before assigning `category_id`. |
| Batch update (PR 3) | new `bulk_update` service | Inherits guard by looping single-update. |
| Reconciliation edit | `reconciliation_service.apply_edits_to_reconciliation_row` (~498) | Ensure guard applies if the row is a transfer leg. |
| Recurring series sync | `transaction_service._propagate_fields_to_series` (~429/439) | Only touches PENDING non-transfer rows; confirm it cannot target a transfer leg. No change expected. |
| Create / pair / link | `validate_transfer_category` + `_link_pair` | Already enforce `both` on create; no change. |

Prefer a single shared helper (e.g. extend/compose `validate_category_for_type`
with a transfer-aware check) so the rule lives in one place and cannot drift.

### Backend — partner sync

The list renders a transfer as **one visible row**; the partner leg is filtered
out of the view (`selectionHiddenIds`, higher-ID leg). So editing the visible
row's category must **cascade to the partner leg**, or the pair desyncs (hidden
leg keeps the old category). Today `update_transaction` writes only the edited
row. Add: when the edited row is a transfer leg and `category_id` changes,
write the same `category_id` to the partner leg in the same transaction.

### Frontend

In the transactions edit row, pass `typeFilter="BOTH"` (the uppercase `both`
filter) instead of `filterType={editType}` when the row being edited is a
transfer leg (`linked_transaction_id != null`). The picker then offers only
`both` categories — recategorizing a transfer stays possible, but only among
`both` categories.

### Out of scope

Editing a master category's `type` (e.g. converting "Debit & Repayments" to
`both`). Explicitly dropped — the owner uses the dedicated "Transfer" category
instead.

### Tests

- Backend: `update_transaction` on a transfer leg rejects a non-`both` category
  (400); accepts a `both` category; the accepted change cascades to the partner
  leg. Regular (non-transfer) transactions are unaffected.
- Backend: a bulk/reconciliation path cannot set a non-`both` category on a
  transfer leg.
- Frontend: editing a transfer row shows only `both` categories; editing a
  regular row shows type-appropriate categories as before.

---

## PR 2 — Remove redundant in-page add button; port "Make recurring" (`feat`)

### Problem

The header quick-add (`components/AppShellAddTransactionCta.tsx` → slide-in
`components/floating/TransactionForm.tsx` / `TransferForm.tsx`) is already
present on the transactions page. The in-page "+ New Transaction" button + inline
form (`frontend/app/transactions/page.tsx:1000-1212`) duplicates it. Two buttons
with the same function on the same page is confusing.

The only capability the inline form has that the header quick-add lacks is the
**"Make recurring" / Repeats** controls (frequency + auto-settle), which the
header `TransactionForm` deferred.

### Goal

One add entry point on the page (the header quick-add), with no loss of
capability.

### Changes

1. Delete the in-page "+ New Transaction" button and its inline form card
   (transaction + transfer modes) from `transactions/page.tsx`, plus now-unused
   local state/handlers (`showForm`, inline submit handlers, etc.).
2. Port the "Make recurring" controls into the shared `TransactionForm`
   component used by the header quick-add: a "Repeats" toggle revealing
   frequency + auto-settle, wired to the same create payload the inline form
   used. Keep the existing "Save and add new" behavior.
3. Transfer creation stays available via the header menu's "New transfer".

### Tests

- Frontend: the in-page add button/form is gone; the page still loads and lists
  transactions.
- Frontend: header quick-add `TransactionForm` exposes the Repeats controls and
  creates a recurring transaction (same payload contract as the old inline
  form).

### Notes

Confirm no other surface depends on the inline form's DOM/test ids before
deleting; update or remove affected tests.

---

## PR 3 — Batch edit (`feat`)

### Problem

The page supports multi-select + bulk **delete**
(`transactions/page.tsx:946-991` toolbar → `POST /api/v1/transactions/bulk-delete`
→ `transaction_service.bulk_delete_transactions`), but there is no bulk
**edit**. Recategorizing many transfers means editing one row at a time.

### Goal

Add a "Batch edit" action to the existing selection toolbar that edits the
selected rows in one operation. Each field is optional — applied only when set.

### Frontend

- Add a "Batch edit" button to the sticky selection toolbar (next to "Delete
  selected").
- It opens a small form / panel with optional fields: **Category**, **Status**,
  **Account**, **Tags**.
- On submit, `POST /api/v1/transactions/bulk-update` with the selected ids and
  only the fields that were set.
- Show a partial-success summary using the response (updated vs skipped, with
  reasons), reusing the bulk-delete result-messaging pattern. Refresh the list.

### Backend

New endpoint `POST /api/v1/transactions/bulk-update`:

- Request: `{ ids: list[int] (deduped, capped like bulk-delete), category_id?,
  status?, account_id?, tags? }`.
- Implementation: loop the selected rows through the existing single-update
  service so all validation, the PR 1 transfer guard, partner sync, and balance
  bookkeeping apply uniformly. Partial-success: collect `skipped_ids` with
  reasons instead of failing the whole batch.
- Response: `{ requested_count, updated_count, skipped_ids }` (mirror
  `BulkDeleteResponse`).

### Transfer handling in batch

| Field | Transfer-leg behavior |
|-------|----------------------|
| Category | Must be `both` (PR 1 guard); cascades to partner leg. Non-`both` target → those transfer rows go to `skipped_ids` with a reason. |
| Status | Applies to both legs (kept in sync). |
| Account | Ambiguous for a transfer (it is one side of the transfer) → transfer legs **skipped** for account changes, reported. |
| Tags | Transfers do not support tags → transfer legs **skipped** for tags. |

### Field semantics (regular transactions)

- **Tags:** v1 = **add/merge** the given tags onto each selected row (no
  removal). (Replace semantics can be a later iteration.)
- **Account change:** for settled rows, reuse the bulk-delete balance-revert
  pattern — revert the old account's balance and apply to the new account.
- **Status / Category:** same validation as a single edit.

### Tests

- Backend: bulk-update sets category on a mix of regular + transfer rows;
  transfer rows accept a `both` category (cascaded to partners) and are skipped
  for a non-`both` category, account, and tags; partial-success counts correct.
- Backend: bulk account change reverts/applies balances for settled rows.
- Frontend: selecting rows + batch-editing category updates the list; skipped
  rows are surfaced.

### Notes

- Selection already collapses transfer pairs to one visible row and locks
  partners together — batch edit operates on the visible selection; partner
  legs are handled in the service (category cascade, status sync).
- Cap `ids` consistently with bulk-delete to bound the operation.
