---
name: Credit Card Account Model Upgrade
description: 2026-05-15 owner backlog. Critical assessment of a suggested CC model upgrade — keeps the useful pieces (credit limit, statement closing/due day, payment source linkage) and rejects the bank-replication pieces (cron-based statement reset, interest accrual, computed min payment).
type: project
originSessionId: 31bd894a-67ce-4301-b8b1-880672646504
---
# Credit Card Account Model Upgrade

**Captured 2026-05-15.** Discussion-grade spec; pre-implementation. Owner-locked principle: **PFV is a personal-decision planning tool, not a bank/billing system.** Most "compute everything" suggestions trip that wire.

## Owner-stated motivation

Today the credit-card account treats cards roughly like checking accounts with a single "due date" field. The user wants:
- Visibility into credit health (utilization, available credit)
- Better forecasting — when the statement closes, what's going out of the source account, on what day
- Reasonable upgrade from "one date" to a richer cycle model

## What the suggested design proposed

The suggestion (verbatim user message) included these elements:
- New fields: Credit Limit, Statement Closing Date, APR, Grace Period
- Computed metrics: Utilization Rate, Statement Balance, Minimum Monthly Payment, Interest Accrual
- UI: limit + APR + closing day + due day on creation
- A cron job on Statement Closing Day to "calculate the new Statement Balance, clear the previous month's history, and set the next Due Date"

## Critical assessment

### What's right (keep)

1. **Credit Limit** is a real attribute. Without it, the dashboard cannot show "available credit" or "utilization %", both of which are real personal-finance signals.
2. **Statement closing day + payment due day** are real per-card attributes. Replacing the single "due date" field is the right shape.
3. **Statement balance** as a *displayed* concept (sum of settled transactions in the closed cycle, currently visible vs frozen) is useful.
4. **Utilization metric** (balance / limit) is trivial to compute on-demand from existing data once `credit_limit` exists.

### What's overengineered or wrong-by-construction (reject)

1. **Cron-based statement reset.** No. PFV does not need a cron job mutating financial data. Statement closing is a *bank* event we observe; we don't reproduce it. The closed-cycle view we want is a computed *read* (sum of settled transactions in the cycle), not a mutated snapshot. Cron mutations of financial state are an incident magnet and we have none in production right now. Keep it that way.
2. **Computed interest accrual.** PFV cannot accurately compute interest. Banks use daily-average-balance math, issuer-specific grace-period rules, posting-date vs transaction-date distinctions. We don't have those signals from a CSV/OFX import. Trying to "compute interest" produces wrong numbers the user will rely on. The user's bank statement IS the source of truth for interest; PFV's job is to surface what the user owes when, not invent the math.
3. **Computed minimum payment.** Same class of error. Min payment is on the user's statement; we should READ it (if the user enters it), not COMPUTE it. The proposed `max($25, interest + 1% of balance)` is one issuer's formula, not universal.
4. **Grace Period as a stored field.** It's derivable from `closing_day → due_day` distance. No separate field needed.
5. **APR as a *required* field.** Optional metadata is fine for the user's own tracking, but PFV doesn't use it for any computed output. Don't force the user to enter it.

### What's missing from the suggested design (add)

1. **Payment source account linkage** (cross-cutting with Loan spec — see `project_loan_account_type.md`). The bill gets paid from somewhere. If the user can declare "Citi Mastercard's statement balance gets paid from BBVA Checking", the forecast can correctly drop BBVA Checking's projected balance on the due day. This is the single most valuable visibility upgrade — and the user explicitly asked for it.
2. **Payment strategy.** Some users pay full balance every cycle (and never pay interest), some pay the minimum + carry balance, some pay a custom fixed amount. The strategy affects how much leaves the source account on the due date. Model:
   - `payment_strategy: enum('full_balance' | 'minimum_only' | 'fixed_amount' | 'custom_per_period')`
   - For `fixed_amount`: a `fixed_payment_amount Decimal`
   - For `custom_per_period`: the user enters the amount per cycle as they go
3. **Cycle anchoring decision.** Today PFV groups all transactions by `Organization.billing_cycle_day` (per-org monthly anchor). Credit cards have per-card closing days that may differ. Open product question: do we (a) keep grouping CC transactions by the org's anchor (simpler, less honest about real cycles), or (b) group CC transactions by the CC's own closing day (more honest, requires per-account period bucketing)? **Lean (a) for V1**, surface (b) as a future enhancement if users complain.

## Recommended V1 scope

### Schema additions (`accounts` table)

| Field | Type | Notes |
|---|---|---|
| `credit_limit` | DECIMAL(12,2) NULL | NULL on non-credit accounts |
| `statement_closing_day` | TINYINT NULL | 1-31 (handle months with fewer days; 31 → last day of month) |
| `payment_due_day` | TINYINT NULL | 1-31, same handling |
| `payment_strategy` | ENUM NULL | `full_balance` (default for CC) / `minimum_only` / `fixed_amount` / `custom_per_period` |
| `fixed_payment_amount` | DECIMAL(12,2) NULL | Required if `payment_strategy='fixed_amount'` |
| `payment_source_account_id` | INT NULL FK | FK to `accounts.id` SET NULL; same-org constraint; only allowed for credit/loan account types |
| `apr` | DECIMAL(5,2) NULL | Optional metadata, no computed use V1 |

Migration shape: one alembic file, ~30 lines, single ALTER TABLE.

### Validation rules

- `credit_limit`, `statement_closing_day`, `payment_due_day` are required for accounts whose `account_type.slug === 'credit_card'`. Not required for other account types.
- `payment_source_account_id` constraints:
  - Must be same org
  - Source account's `account_type.slug` must be in `('checking', 'savings')` — not another credit card, not a loan
  - Cannot equal `self.id` (no self-pay)
- `statement_closing_day` and `payment_due_day`: 1-31, integer. Day-31 wraps to last day of month (we ALREADY handle this for `billing_cycle_day`; reuse `lib/date_utils.py`'s end-of-month logic).

### UI changes

**Account create/edit form (`/accounts`):**
- When `account_type.slug === 'credit_card'`:
  - Replace single "due date" field (if present today) with: credit limit (number), statement closing day (1-31 dropdown), payment due day (1-31 dropdown), payment source account (account picker, owner+admin scope), payment strategy (radio), fixed payment amount (conditional), APR (optional number)
  - The picker for source account excludes the current CC + other CCs + loan accounts

**Account detail view:**
- Show credit limit + current balance + computed utilization (visible only on credit cards)
- Show "Paid from: <source account name>" line
- Show next statement period at-a-glance (computed from closing day): "Cycle closes Oct 5, due Oct 26"

### Forecast service changes

When the forecast service computes a credit-card account's projected outflow:
- If `payment_source_account_id` is set:
  - Determine projected statement balance for the cycle ending on the next `statement_closing_day` (sum of settled tx in cycle)
  - Apply `payment_strategy` to determine outflow amount (full | minimum | fixed | custom)
  - On `payment_due_day`, debit the source account by that amount in the forecast view
  - The credit card account itself shows "Paid: $X" on the due day, balance returns to current outstanding minus payment
- If `payment_source_account_id` is NULL: current behavior (user manually models payment as a recurring transaction)

### Out of scope V1

- Interest accrual computation. User enters interest manually as a transaction if they want it tracked.
- Min payment computation. User enters min payment manually IF their `payment_strategy === 'minimum_only'`. (Or: prompt them at statement-cycle close to enter it as the next month's outflow.)
- Frozen statement balance snapshot. Statement balance is a computed view, not a stored field.
- Per-CC cycle bucketing. Transactions still group by org billing-cycle-day in V1.
- Cron jobs of any kind.

## Recurring expense suggestion (user's secondary note)

User suggested CC bills should appear in the recurring list. **Partially agree.** Two ways:

1. **Auto-create a recurring transaction** when the user sets up a CC with a `payment_source_account_id` and a `payment_strategy !== 'custom_per_period'`. The recurring is created on the source account, monthly, amount = expected payment, category = "Debt Payment". Pros: visible in the recurring list, drives existing forecast logic. Cons: amount drifts each cycle for `full_balance` strategy; needs auto-update.

2. **Treat payment as a derived forecast item** (not a recurring transaction). The forecast service synthesizes the outflow each cycle from the CC's settings + cycle balance. Pros: amount is always correct. Cons: doesn't appear in the recurring list.

**Recommendation: (2).** The recurring list is for user-defined recurring expenses. CC payments are *bills derived from current usage*, not fixed recurring expenses. They belong in the forecast as a synthesized item with provenance `source=credit_card_payment`. The user can SEE them in forecast detail; they don't clutter the recurring list with auto-generated rows whose amounts they didn't choose.

## Open product questions (for discussion)

1. **Cycle anchoring**: org-level billing cycle OR per-card closing day for transaction grouping?
2. **Statement balance handling**: pure computed view, OR snapshot at close for historical audit?
3. **Min payment**: never tracked, OR optional user-entered field for `minimum_only` strategy?
4. **APR**: drop entirely from V1, OR keep as optional metadata for users who want to see "you have a high-APR card"?
5. **Multiple payment sources**: V1 single source per CC OR allow splits ("pay 50% from checking, 50% from savings")?
6. **Linked-asset accounting on payment**: when the user records a payment from checking → CC, do we automatically create a *transfer pair* (existing PFV primitive) between the two accounts? Or keep them as two separate transactions? Lean: transfer pair, since PFV already has `linked_transaction_id` + transfer detection.

## Effort estimate

- Schema (CC-specific columns only — `payment_source_account_id` ships in the foundation slice) + validation + form changes + forecast integration: **M (2-4 days)**
- Doc updates + audit-event coverage on the new fields: **XS-S**
- Total: **M**, single-PR feasible if scope is held tight to V1.

## Priority + sequencing (architect-locked 2026-05-15)

**P2 pre-launch**, second in the financial-primitives stack:

1. Foundation: `payment_source_account_id` plumbing — see `project_payment_source_account_foundation.md`
2. **THIS SLICE: Credit Card model V1**
3. Dashboard Phase 0 per-type tiles — independent of foundation
4. Loan V1 — deferred unless central to target-user first impression
5. Configurable dashboard widget framework — post-launch

**Updated bundling recommendation (supersedes earlier "consider bundling with Loan"):** do NOT bundle CC and Loan UX in one PR. Ship the foundation first; then CC UX as its own PR; then Loan UX as its own PR. Architect rationale: keeps each review small, lets either liability UX ship without blocking the other, and decouples liability models in case one is reworked.

**Schema placement of the shared field**: deferred to the foundation slice's decision memo (`accounts.payment_source_account_id` directly vs new `liability_terms` child table). This CC spec assumes the architect-approved placement; do not re-decide here.

## Cross-references

- `project_payment_source_account_foundation.md` — prerequisite slice; consumes nothing from here
- `project_loan_account_type.md` — companion liability spec; same dependency on foundation
- `project_billing_cycle.md` — existing billing-cycle-day work; cycle-anchoring decision here may depend on what's there
- `project_user_billing_flow.md` — user's salary-anchored period thinking; CC cycles may want to honor that
- L3 roadmap section (financial primitives) — natural fit for an L3.x row if this gets approved
