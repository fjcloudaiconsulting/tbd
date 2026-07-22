---
name: Payment Source Schema Placement Decision Memo
description: Foundation-PR deliverable required by specs/payment-source-account-foundation.md — resolves V1A (column on accounts) vs V1B (liability_terms child table). Decision: V1A.
type: project
---
# Decision Memo — `payment_source_account_id` schema placement

**Required by** `specs/payment-source-account-foundation.md` § "Open decision". Resolve before implementing.

**Decision: V1A — `accounts.payment_source_account_id` (column directly on `accounts`).**

## The question

Both options carry the same logical field, a nullable self-referential FK. The
difference is whether liability-specific metadata lives on the parent
`accounts` row (V1A) or in a 1:0..1 child table keyed by `account_id` (V1B).

## What the code actually shows (correcting the spec's assumptions)

The spec's cons for V1A leaned on the fear that `accounts` would accumulate
"~10 NULL columns". Reading the current schema, that fat-account-row pattern is
**already the established, shipped idiom** — not a hypothetical:

- `backend/app/models/account.py:62-64` — `close_day`, `payment_day`,
  `payment_day_relative_month` are all `nullable=True`, **credit-card-only**
  columns living directly on `accounts`. They are NULL for every checking,
  savings, cash, and investment account today, and nobody has complained.
- The CC billing-cycle feature (migrations for `close_day` / `payment_day`,
  shipped May 2026) chose a column-on-`accounts` home for exactly the
  liability-metadata class this memo concerns. Introducing `liability_terms`
  now would create **two competing homes** for CC metadata: the existing
  `close_day`/`payment_day` on `accounts`, and a new child table. The CC-model
  upgrade slice (`specs/credit-card-model-upgrade.md`) would then have to
  either split its fields across both or migrate the existing columns — churn
  with no user-visible benefit.
- Validation already lives in a service layer (`account_type_change_service.py`)
  with the "field X is only allowed on credit_card accounts" invariant enforced
  in code, **not** in the schema. V1B's headline pro — "schema-level expression
  of liability-only fields" — is not something this codebase relies on or wants;
  the project has deliberately kept that rule in the service layer.

Corrections to the spec's framing worth recording for downstream slices:

- **There is no `loan` account type.** `SYSTEM_ACCOUNT_TYPES`
  (`backend/app/models/account.py:20-26`) is `checking, savings, credit_card,
  investment, cash`. The "Paid from" picker therefore triggers on `credit_card`
  only for now; `loan` is added when `specs/loan-account-type.md` lands.
- **`billing_cycle_day` / `allow_manual_balance_adjustment` are on
  `Organization`, not `accounts`** — the spec cited them as `accounts` columns.
  The genuine `accounts`-level precedents are `close_day` / `payment_day` /
  `payment_day_relative_month`.
- **The update endpoint is `PUT /{id}`**, using the `model_fields_set` idiom to
  distinguish "omitted" from "explicit null".

## Recommendation & rationale

**V1A.** It matches the shipped idiom exactly, keeps CC metadata in one home,
needs one additive `ALTER TABLE` (mirroring migration `041_opening_balance`),
and requires no join on the hot "list accounts" path. V1B's cons (mandatory
join on the common detail path, two-step transactional writes, a brand-new
pattern future authors must learn) are real costs; its pros (clean parent row,
cascade scoping) are already satisfied in V1A by `ON DELETE SET NULL` on the FK
and by the fact that the fat-row pattern is the accepted norm here.

The asymmetry-tolerance the spec worried about is already the reality: most
accounts have NULL `close_day`/`payment_day` and that has been fine.

## Migration sketch (chosen option, V1A)

New revision `072_payment_source_account_id`, `down_revision = "071_api_tokens"`
(current head). Additive, ~10 lines, no backfill (all rows start NULL):

```python
def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("payment_source_account_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_accounts_payment_source_account_id",
        "accounts",
        "accounts",
        ["payment_source_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

def downgrade() -> None:
    op.drop_constraint(
        "fk_accounts_payment_source_account_id", "accounts", type_="foreignkey"
    )
    op.drop_column("accounts", "payment_source_account_id")
```

Note: a self-referential FK on `accounts(id)` needs an index on the FK column
for MySQL (errno 1553 class — see `reference_mysql_fk_index_cover`). MySQL
auto-creates a covering index for the FK constraint, so no explicit
`create_index` is required; the `create_foreign_key` above is sufficient. On
SQLite (test engine) the constraint is created via batch/`ADD COLUMN` and FK
enforcement is gated by `PRAGMA foreign_keys=ON` (already set in the test
harness), so `ON DELETE SET NULL` is exercised by the deletion test.

## Validation (service layer, matching existing convention)

New `app/services/payment_source_service.py`, mirroring
`account_type_change_service.validate_create_*`. When
`payment_source_account_id` is set (non-null):

1. Source exists in the same `org_id` → else 422 ("entity-not-for-you", matching
   the type-change service's cross-org convention).
2. Source `!=` target (self-pay) → 422. Skipped on create (new account has no id).
3. Source `account_type.slug ∈ {checking, savings, cash}` → else 422.
4. Source `is_active is True` → else 422.

`ON DELETE SET NULL` handles later deactivation-by-deletion automatically; a
deactivated-but-not-deleted source keeps the FK intact and the UI surfaces an
"inactive source" hint at read time (client resolves from the accounts list).

## Owner sign-off

Recommendation: **V1A**. Implemented in this PR.

Owner sign-off: ________________________  Date: __________
