# Recurring ↔ transaction field sync (name + category)

**Date:** 2026-06-03
**Status:** Spec — ready for implementation plan
**Branch target:** new feature branch off `main`

## Problem

A recurring template and the transactions it generates are fully decoupled
after generation. Each generated instance is a **snapshot copy** of the
template's fields at generation time (`recurring_service.py:303-314`), linked
back only by the informational FK `transactions.recurring_id`
(`transaction.py:90-92`, `ON DELETE SET NULL`). Editing a transaction's name or
category never writes back to the template
(`transaction_service._apply_field_updates`), and the "Recurring" badge in the
transaction list is purely decorative — it doesn't navigate anywhere and gives
no hint that an edit will (or won't) affect the series.

The owner edited a recurring-generated transaction's name and was confused that
the change didn't reflect on the recurring template. The link between an
instance and its origin series is invisible and inert, which reads as a bug even
though the decoupling is intentional.

Separately, the owner initially believed deleting a single transaction also
removes future occurrences. **It does not** — single and bulk delete only remove
the selected rows (`transaction_service.py:706-867`); future *pending* instances
are removed only when the **template** is stopped or deleted
(`_remove_pending_transactions`, `recurring_service.py:147-161`), and settled
history is always preserved. This behavior is **correct and stays as-is** — it
is explicitly out of scope to change.

## Goal

Add a surgical, additive link so that editing a recurring-linked transaction's
**name or category** propagates forward to the series, and so the "Recurring"
badge honestly reflects whether the series still exists. No rearchitecture; the
FK already exists. Document the behavior internally and in the user-facing
manual, and add UI affordances so the sync is discoverable rather than
surprising.

Tags are **deferred** to a follow-up (the template has no tag support today —
that requires a join table + migration + generation copy + recurring-side UI).

## Decisions (from brainstorm 2026-06-03)

- **Ripple scope:** edit propagates to the **template + all PENDING linked
  instances**. SETTLED (past) instances are left untouched (historical fact).
- **Trigger source:** propagation fires from **any** linked transaction (any row
  carrying `recurring_id`), not only a designated origin. No new "origin"
  tracking is needed.
- **Fields in scope:** `description` (name) and `category_id` **only**. Amount,
  account, type, date, status are NOT propagated — they legitimately vary per
  occurrence.
- **Tags:** out of scope for this pass; file as a follow-up.
- **Badge on stop:** clear the link (`recurring_id = NULL`) on surviving rows
  when a template is stopped, mirroring delete's existing FK behavior. (Chosen
  over keep-link-and-read-`is_active`, which preserves history but needs a new
  response field + frontend logic.)
- **Delete behavior:** unchanged. Single/bulk transaction delete never cascades
  to other instances or the template.

## Current behavior (baseline)

- **Generation** (`recurring_service.py:303-314`): copies template
  `account_id`, `category_id`, `description`, `amount`, `type` into a new
  `Transaction` and sets `recurring_id = template.id`. Snapshot — no live
  reference.
- **Transaction edit** (`transaction_service.update_transaction`,
  `:374-604`): `description` applied via `_apply_field_updates` (`:493`);
  `category_id` applied at `:494-495`; `old_category_id` captured at `:454`.
  `recurring_id` is never read or modified. `PUT /transactions/{id}` is the
  **only** mutation path — bulk endpoints are delete-only
  (`routers/transactions.py:473`).
- **Template edit** (`recurring_service.update_recurring`, `:97-144`): affects
  only future generations; existing instances keep their snapshot values.
- **Stop** (`stop_recurring`, `:164-180`): sets `is_active = False`, deletes
  PENDING future instances (`date >= today`), keeps the template row. Surviving
  rows keep `recurring_id` → badge persists.
- **Delete** (`delete_recurring`, `:183-200`): removes PENDING future instances,
  deletes the template row → FK `SET NULL` clears `recurring_id` on survivors →
  badge already disappears.
- **Frontend badge** (`transactions/page.tsx:1528-1534`, `:1816-1822`): static
  "Recurring" chip shown when `recurring_id !== null`. No tooltip, no navigation.

## Proposed behavior

### 1. Field propagation (backend)

In `update_transaction`, after the per-leg field updates are applied
(`transaction_service.py:493-499`):

- Capture the pre-update `description` (alongside the existing `old_category_id`
  at `:454`).
- If `tx.recurring_id is not None`, determine which in-scope fields **actually
  changed**:
  - name changed: `body.description is not None and new_description != old_description`
  - category changed: `body.category_id is not None and new_category_id != old_category_id`
- For each changed field, within the same DB transaction:
  - Update the template row (`recurring_transactions.description` /
    `.category_id`) for that `recurring_id` + `org_id`.
  - Bulk-update all PENDING linked instances:
    `UPDATE transactions SET <field> WHERE recurring_id = X AND org_id = Y AND status = PENDING`.
    (The directly-edited row may be included harmlessly; its value is already
    set.)
- No change → no writes (editing amount-only, or re-saving an unchanged name,
  touches nothing).
- A category propagation must keep the existing
  `validate_category_for_type` guard semantics already enforced on the edited
  row; the template/instances inherit the same validated `category_id`.

### 2. Badge clears on stop (backend)

In `stop_recurring`, after `_remove_pending_transactions`, set
`recurring_id = NULL` on all remaining rows for that `recurring_id` + `org_id`
(same effect delete gets for free via the FK). `delete_recurring` is unchanged.

### 3. Frontend affordances

- **Edit-form hint:** when the transaction being edited has `recurring_id` set,
  render an inline note near the name/category fields:
  *"Editing the name or category also updates this recurring series and its
  upcoming occurrences."*
- **Badge tooltip:** the "Recurring" chip gains a tooltip:
  *"Generated from a recurring series. Name and category stay in sync with the
  series."*

No cross-page live update is needed — the transactions list and `/recurring`
each refetch on navigation, so propagated values and the cleared badge appear
naturally.

### 4. Documentation

- **Internal:** this spec committed under `specs/`; a short code comment at the
  propagation hook; a `codebase_shape.md` changelog note.
- **User-facing:** add/extend a "Recurring transactions" explainer in the in-app
  manual (Next.js `/docs`) covering: instances are independent snapshots;
  editing name/category syncs forward to the template and pending occurrences;
  amounts and dates are per-occurrence; deleting a single transaction never
  removes other occurrences; stopping or deleting the series removes only
  *pending future* occurrences and preserves settled history. Exact `/docs` page
  to be confirmed during planning. (No em-dashes in customer copy per house
  style.)

## Out of scope

- Tag propagation (follow-up: needs `recurring_transaction_tags` join +
  migration + generation copy + recurring-side UI).
- Amount/account/type/date/status propagation.
- Any change to single/bulk transaction delete semantics.
- "This occurrence vs. all" calendar-style scope prompts on edit.
- Navigation from a transaction to its template (badge stays informational +
  tooltip only).

## Testing

**Backend**
- Edit name on a generated instance → template `description` updated; all PENDING
  linked instances updated; SETTLED instances unchanged.
- Edit category likewise (`category_id`), respecting type/category compatibility.
- Edit amount only → no propagation to template or instances.
- Re-save unchanged name/category → no writes.
- Propagation from a non-origin generated instance still updates the series
  (any-linked-transaction rule).
- `stop_recurring` → surviving linked rows have `recurring_id = NULL` (badge
  cleared); pending future removed; settled preserved.
- `delete_recurring` → badge cleared (regression guard for existing FK behavior).

**Frontend**
- Edit-form hint renders only when `recurring_id` is set.
- "Recurring" badge tooltip present.
