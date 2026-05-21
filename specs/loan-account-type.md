---
name: Loan Account Type
description: 2026-05-15 owner backlog. Critical assessment of a suggested Loan account type. Keeps principal/rate/term/origination + amortization formula + payment source linkage. Defers per-payment interest/principal split + full amortization UI to V2.
type: project
---
# Loan Account Type

**Captured 2026-05-15.** Discussion-grade spec; pre-implementation. Companion to `project_credit_card_model_upgrade.md` — both share the `payment_source_account_id` plumbing.

## Owner-stated motivation

Today PFV has no native loan modeling. Users with a fixed-term loan (car, mortgage, personal) model it as a generic account that goes from negative to zero. They lose: payoff-date projection, principal-vs-interest visibility, amortization-aware forecasting.

The user wants a Loan account type that natively understands a fixed lump sum, fixed monthly payment, and a maturation date.

## What the suggested design proposed

The suggestion (verbatim user message) included:
- New fields: Principal Amount, Interest Rate, Loan Term (months), Amortization Type, Origination Date
- Computed: Amortization Schedule (full table), Fixed Monthly Payment via standard PMT formula, Maturation Date
- UI: total borrowed, interest rate, term length, first payment date
- Transactions: log initial disbursement to a linked asset account, every payment splits into interest expense + principal reduction

## Critical assessment

### What's right (keep)

1. **Loans really are different from credit cards.** A loan has a fixed schedule (P, r, n → PMT) and a known maturation date. A CC has a revolving balance. Treating them as the same account-type with different copy is dishonest to the data shape.
2. **Principal, rate, term, origination date** are real attributes with no ambiguity.
3. **PMT formula** is well-understood and deterministic: `PMT = P × r(1+r)^n / ((1+r)^n − 1)` where r is *monthly* rate. We can compute the expected monthly payment with high confidence.
4. **Maturation date** is derivable from origination + term. Shown to user as "Payoff date: 2031-04-15" is a real visibility win.
5. **Disbursement → linked asset account** is correct accounting. Money came from somewhere; the loan liability is offset by that asset's increase.

### What's overengineered for V1

1. **Per-payment interest/principal split.** The standard amortization split (early payments mostly interest, late payments mostly principal) is well-known math, BUT real banks often: charge fees mid-loan, accept extra principal payments that change the schedule, modify the schedule for skip-a-payment programs, adjust for variable rates. V1 should model payments as opaque outflows: balance decreases by the payment amount, no split shown. V2 can add the split as a per-transaction `principal_portion + interest_portion` if users ask.
2. **Full amortization schedule UI.** Generating a 60- or 360-row schedule is cheap (it's pure math). Rendering it usefully in the UI is its own design task (it's a long table that needs sorting, pagination, "what if I pay extra" overlays). Defer to V2 reports tier. V1 surfaces just the *next* payment + the *payoff date*.
3. **"Amortization Type" enum.** Suggested as "Standard Simple Interest". 99% of consumer loans are standard amortization. Don't store a field for a value that has only one value in practice. Add the enum only when we encounter a second amortization type that needs different math.

### What's missing from the suggested design (add)

1. **Payment source account linkage** — same plumbing as the CC spec. Loan payments come from a checking/savings account. The forecast service needs to know which, to drop the source account's projected balance on the payment date.
2. **Variable-rate loans.** The standard PMT formula assumes a fixed rate. Adjustable-rate loans (ARMs) and HELOCs change the rate per period. For V1: only fixed-rate loans are supported; ARM is a flag for "talk to me when we encounter a real user with an ARM."
3. **Early payoff handling.** What if the user pays an extra $5000 toward principal? The remaining schedule changes. V1 should recompute the schedule whenever a payment exceeds the expected PMT (treats overage as extra principal). V2 can let the user explicitly tag transactions as "extra principal."
4. **Outstanding-balance projection.** The user's dashboard should be able to show "loan balance projected at end-of-year = $X". This is a forecast-service integration point.

## Recommended V1 scope

### New AccountType row

Seed a system row via migration:
```
INSERT INTO account_types (org_id=NULL, name='Loan', slug='loan', is_system=TRUE) -- or per-org idempotent insert per existing seeding pattern
```

(Per `codebase_shape.md` §2, AccountType is a per-org table with `is_system` flag. Replicate the existing seeding pattern — each org gets a loan AccountType on bootstrap, OR a global is_system=TRUE row visible to all orgs. Decide based on existing `org_bootstrap_service.seed_org_defaults` shape.)

### Schema additions (`accounts` table — same migration as CC upgrade if bundled)

| Field | Type | Notes |
|---|---|---|
| `principal_amount` | DECIMAL(12,2) NULL | Required for loan accounts |
| `interest_rate_apr` | DECIMAL(5,2) NULL | Required for loan accounts; fixed-rate only V1 |
| `term_months` | SMALLINT NULL | Required for loan accounts |
| `origination_date` | DATE NULL | Required for loan accounts |
| `first_payment_date` | DATE NULL | Required for loan accounts |
| `payment_source_account_id` | INT NULL FK | Shared with CC spec |

(`payment_source_account_id` and the CC fields can share one migration if both designs land together. Bundling avoids two ALTER TABLEs on `accounts` in close succession.)

### Validation rules

- All five loan-specific fields required when `account_type.slug === 'loan'`
- `principal_amount > 0`, `interest_rate_apr >= 0`, `term_months > 0`, `term_months <= 480` (40 years max — covers mortgages)
- `first_payment_date >= origination_date`
- `payment_source_account_id` same constraints as CC spec (same org, source type is checking/savings, not self)

### Computed (no storage)

| Metric | Formula |
|---|---|
| Expected monthly payment | `PMT = P × r(1+r)^n / ((1+r)^n − 1)` where r = APR/12/100, n = term_months, P = principal_amount |
| Maturation date | `first_payment_date + (term_months - 1) months` |
| Payoff date projected (current) | `first_payment_date + ceil(current_balance / expected_monthly_payment) months` — assumes current pace |
| Total interest projected | `(expected_monthly_payment × term_months) - principal_amount` |

All computed on-demand in `app/services/loan_service.py` (new file) and exposed via the account detail endpoint.

### UI changes

**Account create form:**
- When `account_type.slug === 'loan'` is selected, show: principal amount, interest rate (%), term length (months — dropdown 12/24/36/48/60/72/84/120/180/240/360/custom), origination date, first payment date, payment source account picker
- On submit: optional one-click "create the matching disbursement transaction" — credits the source asset account by `principal_amount`, debits the loan account by `-principal_amount`. Use existing transfer-pair primitive (`linked_transaction_id`) so it's auditable and unlinkable later.

**Account detail view:**
- Show: principal, current balance, interest rate, expected monthly payment, maturation date, payoff date projected
- Show: "Paid from: <source account name>" with edit affordance
- Show: next payment ("$X due on Apr 15, 2026")
- Auto-create a recurring transaction template on loan creation? **Yes** — leverage existing `recurring` model (per `codebase_shape.md` §2). The recurring template fires monthly from `first_payment_date`, amount = expected monthly payment, source account = `payment_source_account_id`, category = "Debt Payment". User can edit / pause / delete the recurring if their actual payment differs.

**Dashboard tile (optional V1, more likely V2):**
- "Loans on track" widget — counts loans where projected payoff < maturation, etc.

### Forecast service integration

- When forecasting a future period, look at every loan account
- Project an outflow of `expected_monthly_payment` from `payment_source_account_id` on every `first_payment_date + n months` falling in the forecast window
- Project a balance reduction of `expected_monthly_payment` on the loan account on each payment date
- Source: synthesized forecast item with provenance `source=loan_payment` — same shape as the CC `source=credit_card_payment` proposal

### Out of scope V1

- Per-payment interest/principal split (defer to V2)
- Full amortization schedule UI (defer to V2 reports/insights tier)
- Variable-rate / ARM loans (defer until a user has one)
- Extra-principal-payment overlays / "what if I pay extra" simulator (V2)
- Refinancing flow (V3)
- Multiple loan disbursements (lines of credit) — that's a different account type
- Interest deduction tax tracking (V3+, only relevant in some jurisdictions)

## Cross-references

- `project_payment_source_account_foundation.md` — prerequisite slice for the shared `payment_source_account_id` plumbing
- `project_credit_card_model_upgrade.md` — companion liability spec; same dependency on foundation
- `project_billing_cycle.md` — loan payments should honor the org's billing-cycle-day in the forecast view
- `project_user_billing_flow.md` — salary-anchored period thinking applies
- L3 roadmap section — natural fit for an L3.x row

## Open product questions

1. **Disbursement auto-transaction**: required, optional with a checkbox, or never auto-create? Lean: optional with the checkbox defaulted ON. User who imported their loan from CSV and already has the disbursement won't want a duplicate; the checkbox lets them opt out.
2. **Variable-rate flag**: do we add a placeholder `rate_type` field NOW even though V1 is fixed-rate only? Cheap insurance against later migrations. Lean: YES, add `rate_type ENUM('fixed', 'variable') NOT NULL DEFAULT 'fixed'` and only support `'fixed'` in code paths until a real ARM lands.
3. **Recurring template auto-creation**: cleanest UX if it exists, but what if user changes the source account later? Need to update the recurring template. Add this as a write hook on `accounts.payment_source_account_id` change.

(The earlier "bundle with CC?" question is now resolved: ship separately per architect 2026-05-15 — see Priority section below.)

## Effort estimate

- Schema (Loan-specific columns only — `payment_source_account_id` ships in the foundation slice) + AccountType seed + validation + service + recurring auto-creation + forecast integration: **M-L (3-6 days)**

## Priority + sequencing (architect-locked 2026-05-15)

**P4 pre-launch — DEFERRABLE.** Architect-locked sequencing places this fourth in the financial-primitives stack:

1. Foundation: `payment_source_account_id` plumbing — see `project_payment_source_account_foundation.md`
2. Credit Card model V1 — see `project_credit_card_model_upgrade.md`
3. Dashboard Phase 0 per-type tiles — independent of foundation
4. **THIS SLICE: Loan account V1**
5. Configurable dashboard widget framework — post-launch

**Defer unless loans are central to target-user first impression.** Architect rationale: loans are a bigger product surface than CC, and most beta users don't have a primary loan to track. CC affects monthly cash-flow ambiguity more heavily and ships sooner. Loan V1 stays specced and ready, but no commit to ship pre-launch unless the product positioning explicitly needs it.

**Do NOT bundle with CC.** Each liability UX ships as its own PR after the foundation lands, so one liability model can't block the other in review and either can be reworked without touching the other.

**Schema placement of the shared field**: deferred to the foundation slice's decision memo (`accounts.payment_source_account_id` directly vs new `liability_terms` child table). This Loan spec assumes the architect-approved placement; do not re-decide here.

Becomes more valuable as a feature differentiator if shipped post-launch (compared to YNAB / Monarch / Copilot — none of them model loans well today).
