---
name: Payment Source Account Foundation
description: 2026-05-15 architect-locked sequencing — ship the shared payment_source_account_id plumbing as its own PR before any CC or Loan UX work. Foundation only. No payment automation, no generated transactions, no cron jobs.
type: project
originSessionId: 31bd894a-67ce-4301-b8b1-880672646504
---
# Payment Source Account Foundation

**Captured 2026-05-15.** Architect-locked sequencing decision: this slice ships FIRST, before any of the financial-primitives liability UX (Credit Card model upgrade, Loan account type). The CC and Loan UX slices both depend on this plumbing; landing it independently lets each liability UX ship without blocking the other in review.

## Architect-locked scope (verbatim)

> Scope:
> - Add nullable `payment_source_account_id` to account/liability metadata where appropriate.
> - Validation: same org, source account must be checking/savings/cash-like, source cannot equal target account.
> - No payment automation, no generated transactions, no cron jobs.
> - Expose the field in backend schemas and account settings UI only where relevant.
> - Add tests for validation, org isolation, deletion/deactivation behavior, and forecast read compatibility.
> - Include a short decision memo on whether this belongs directly on accounts or in a liability-specific metadata table before implementing.
>
> Out of scope:
> - Credit card statement cycles
> - Loan amortization UI
> - Minimum payment / interest computation
> - Configurable dashboard widgets
> - Auto-created recurring rows

## Open decision (resolve before implementing)

**Schema placement: `accounts.payment_source_account_id` (V1A) vs new `liability_terms` child table (V1B)?**

The foundation PR must produce a short decision memo on this question first. Both options carry the same logical field — the difference is whether liability-specific metadata lives directly on the parent `accounts` row or in a 1:0..1 child table keyed by `account_id`.

### V1A: directly on `accounts`

Pros:
- Simple, denormalized; fast reads (no join for the common "account detail" page)
- Matches the existing PFV convention (`billing_cycle_day`, `allow_manual_balance_adjustment`, opening-balance fields all live directly on `accounts` / `organizations`)
- One ALTER TABLE; small migration surface
- Easy to query in admin tools without joining

Cons:
- `accounts` accumulates nullable columns: `payment_source_account_id`, plus eventual CC fields (`credit_limit`, `statement_closing_day`, `payment_due_day`, `apr`, `payment_strategy`, `fixed_payment_amount`), plus eventual Loan fields (`principal_amount`, `interest_rate_apr`, `term_months`, `origination_date`, `first_payment_date`, `rate_type`). For an asset account (checking/savings/cash) none of those apply, leaving most rows with ~10 NULL columns.
- Schema-level inability to express "these fields are required IFF account is a credit card" — has to live in service/validation layer

### V1B: new `liability_terms` table

Shape sketch:

```sql
CREATE TABLE liability_terms (
  account_id INT PRIMARY KEY,
  payment_source_account_id INT NULL,
  -- credit card fields (added when CC spec lands)
  credit_limit DECIMAL(12,2) NULL,
  statement_closing_day TINYINT NULL,
  payment_due_day TINYINT NULL,
  apr DECIMAL(5,2) NULL,
  payment_strategy ENUM(...) NULL,
  fixed_payment_amount DECIMAL(12,2) NULL,
  -- loan fields (added when Loan spec lands)
  principal_amount DECIMAL(12,2) NULL,
  interest_rate_apr DECIMAL(5,2) NULL,
  term_months SMALLINT NULL,
  origination_date DATE NULL,
  first_payment_date DATE NULL,
  rate_type ENUM('fixed','variable') NULL,
  FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
  FOREIGN KEY (payment_source_account_id) REFERENCES accounts(id) ON DELETE SET NULL
);
```

Pros:
- `accounts` stays clean; the parent row's columns describe what every account has, not what some accounts might have
- Cascading delete is naturally scoped to the liability metadata
- A liability-specific service layer has one obvious source of truth

Cons:
- Every "show me the credit card detail" path needs a join — most callers care about it; usage frequency is high
- The PFV pattern leans toward fat-account-row (per existing code) — this would be a new pattern that future authors have to know about
- Two-step writes (insert account + insert liability_terms) need transactional discipline

### Decision-memo deliverable

Foundation PR must produce a 1-page decision memo capturing:
- The two options laid out above (or any third option the implementer discovers)
- A recommendation with rationale referencing PFV's existing schema patterns (`codebase_shape.md` §2: list of fields on `accounts` and `organizations` to compare against)
- Migration sketch for the chosen option
- Owner sign-off line at the bottom

Default lean if no strong signal emerges: **V1A** (directly on `accounts`), because it matches existing PFV idiom and the asymmetry-tolerance pattern is already established (most accounts won't have `allow_manual_balance_adjustment=TRUE` either, and that's a single column with no complaints). Resolve in the memo.

## Concrete scope of the foundation PR

### Schema (one column, on the chosen home)

- `payment_source_account_id INT NULL` FK to `accounts.id` ON DELETE SET NULL
- New alembic migration, ~10 lines

### Validation (`accounts_service.py` or equivalent)

When `payment_source_account_id` is set or updated:
1. Source account must exist in the same `org_id` as the target account
2. Source account's `account_type.slug` must be in an allowlist: `checking`, `savings`, `cash`. NOT `credit_card`, NOT `loan`. Resolve via join to `account_types`.
3. Source account cannot equal target (no self-pay): `source_id != target.id`
4. Source account must be active (`is_active=TRUE`) at write time. After write, if source is later deactivated, the FK SET NULL handles automatic cleanup — call it out in the deletion test.

### Backend schema exposure

- `AccountResponse` Pydantic schema gains optional `payment_source_account_id: int | None`
- `AccountCreate` and `AccountUpdate` schemas accept it; null clears, omit preserves (use the project's existing `model_fields_set` idiom — see `codebase_shape.md` §3)
- Routes that already serialize `Account` (currently `GET /api/v1/accounts`, `POST /api/v1/accounts`, `PATCH /api/v1/accounts/{id}`) inherit the field for free

### UI exposure (minimal, V1)

- Account edit form: show a "Paid from" picker on accounts whose `account_type.slug` is `credit_card` OR `loan`. Picker shows other accounts of allowlisted types in the same org. Null option = "(none)".
- Account detail: show "Paid from: <source name>" line if set.
- DO NOT add to dashboard yet (forecast integration is the CC/Loan UX slices).

### Tests

Five tests minimum on the service layer:

1. **Same-org validation**: setting `payment_source_account_id` to an account in a different org returns 400/422 (depending on existing error class)
2. **Org isolation read**: `GET /api/v1/accounts` for org A never surfaces a `payment_source_account_id` pointing at an org B account (currently true by construction since accounts are org-scoped; pin it)
3. **Type allowlist**: setting source to a credit_card or loan account returns 422
4. **Self-pay prevention**: setting source to the account itself returns 422
5. **Deletion/deactivation behavior**: deleting the source account leaves the target with `payment_source_account_id=NULL` (FK SET NULL); deactivating the source account leaves the FK intact but a separate test asserts UI/service surfaces a "source is inactive" warning at read time

Plus one schema-level test: existing forecast reads continue to compile and return the same shape with the new field present.

## Out of scope (architect-locked)

- Credit card statement cycles → `project_credit_card_model_upgrade.md`
- Loan amortization UI / PMT computation → `project_loan_account_type.md`
- Minimum payment / interest computation (both rejected per CC spec)
- Configurable dashboard widgets → `project_configurable_dashboard_widgets.md`
- Auto-created recurring rows
- Forecast service integration (this is the CC and Loan UX slices' job to consume the field)

## Effort estimate

- Schema + validation + backend schema exposure + minimal UI + tests + decision memo: **S (1-2 days)**

## Priority

**P2 pre-launch**, per architect 2026-05-15. The foundation by itself improves nothing user-visible; its value is unlocking the CC and Loan UX slices without coupling them.

## Sequencing (architect-locked 2026-05-15)

| Slice | Dependency | Status |
|---|---|---|
| 1. Foundation (this memo) | — | not started |
| 2. Credit Card model V1 | depends on foundation | spec frozen; awaiting foundation |
| 3. Dashboard Phase 0 per-account-type tiles | independent (no foundation dependency) | spec frozen in `project_configurable_dashboard_widgets.md` Phase 0 |
| 4. Loan account V1 | depends on foundation | spec frozen; deferred unless central to target user impression |
| 5. Configurable dashboard widget framework | depends on Phase 0 validation | post-launch |

## Cross-references

- `project_credit_card_model_upgrade.md` — consumes `payment_source_account_id` in CC UX
- `project_loan_account_type.md` — consumes `payment_source_account_id` in Loan UX
- `project_configurable_dashboard_widgets.md` Phase 0 — independent of this slice
- `codebase_shape.md` §2 (models / `accounts` field list), §3 (`AccountUpdate` partial-update idiom)
