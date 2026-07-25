# Loan Account Type V1 — implementation design

**Date:** 2026-07-24
**Status:** design, architect-reviewed (2 independent grounded reviews folded), pending architect sign-off of this spec.
**Supersedes for implementation:** the discussion-grade `specs/loan-account-type.md` (2026-05-15). That file's product intent stands; this file is the code-accurate plan reconciled against the post-CC-V1 codebase.
**Companion patterns:** CC Model V1 (`specs/2026-07-22-cc-model-v1-design.md`), Payment Source Foundation (`specs/payment-source-account-foundation.md`).

## 1. Motivation

Users with a fixed-term loan (car, mortgage, personal) model it today as a generic account drifting from negative to zero. They lose payoff-date projection and maturation visibility. Loan V1 makes loans a first-class liability type with a contractual schedule (P, r, n → PMT), a maturation date, a live-balance payoff projection, and cash-flow forecast integration for the payment.

## 2. Locked decisions (owner + architect)

1. **Lean, sliced like CC V1.**
   - **Slice 1** (one PR): account type + migration/backfill + 5 loan columns + `validate_loan_fields` + **type-change cascade restructure** + computed metrics + read-only display. Shippable and useful alone (delivers the headline payoff/maturation value).
   - **Slice 2** (one PR): forecast synthesis.
   - **Deferred to explicit follow-up PRs, out of V1:** one-click disbursement transaction, auto-created recurring payment template.
2. **Forecast = CC-clone conserving synthesis** (architects reversed the earlier "hybrid amortization-aware" pick). On each scheduled payment date: `source.expected -= PMT`, `loan.expected += min(PMT, outstanding)` (full amount toward zero). `Σ synth == 0`, preserving `account_balance_forecast_service`'s same-currency total conservation. Contractual payoff/maturation are shown as read-only **metrics**, not as forecast deltas. Interest is not modeled in V1; the true amortization-aware hybrid returns in V2 alongside the per-payment principal/interest split.
   - *Rationale:* the hybrid (`loan += principal_portion` only) makes `Σ synth == −interest ≠ 0`, which breaks the conservation `account_balance_forecast_service.py` depends on (header comment ~:110-116; CC applies equal-and-opposite deltas ~:176-177), and diverges every period from how payments are actually recorded (opaque full amount). Can't defer the split and ship a split-aware forecast.
3. **Skip `rate_type`.** Fixed-rate only, implicit. Pre-launch, no backcompat burden; add the column if a real ARM appears.
4. **Dedicated `interest_rate_apr` column**, not reuse of CC's `apr`. Reuse would let CC's leave-CC cascade null the loan's rate on CC→loan (`account_type_change_service.py:480`) and run the loan rate through CC's `[0,100]` validator (`credit_card_service.py:86`).
5. **Mid-life loan import allowed.** `opening_balance` (current owed, negative) is independent of `principal_amount` (original contractual principal, positive). PMT/maturation derive from the contractual terms; the payoff projection is solved from the **live** balance. No `opening_balance == −principal_amount` enforcement. Documented as: *principal_amount = original principal; schedule is contractual-from-origination; opening_balance = amount currently owed.*

## 3. Slice 1 — data model, validation, cascade, metrics, display

### 3.1 New system account type

- Add `{"slug": "loan", "name": "Loan"}` to `SYSTEM_ACCOUNT_TYPES` (`backend/app/models/account.py:22-28`). This covers **new** orgs (registration → `seed_org_defaults`, existence-guarded on `existing_at_slugs`, `org_bootstrap_service.py:79-87`) and **reset** orgs (reset re-runs the same seed).
- **Existing orgs** get the type via a data backfill in migration 077 (below).

### 3.2 Migration `077_loan_account_type`

Revision id `077_loan_account_type` (21 chars, fits VARCHAR(32) per #573), down_revision `076_cc_statement_category`.

**a) Columns on `accounts`** (all nullable, loan-only; plain types → no ENUM/collation landmine, SQLite CI mirrors MySQL prod faithfully):

| Column | Type | Notes |
|---|---|---|
| `principal_amount` | `Numeric(12,2)` NULL | original principal, > 0 |
| `interest_rate_apr` | `Numeric(5,2)` NULL | annual %, ≥ 0, ≤ 999.99; fixed-rate only |
| `term_months` | `SmallInteger` NULL | 1..480 |
| `origination_date` | `Date` NULL | |
| `first_payment_date` | `Date` NULL | ≥ origination_date |

**b) System-type backfill into existing orgs.** Copy the proven pattern in `037_categories_floor_backfill.py:262-345` (raw SQL on `op.get_bind()`, per-org existence-guarded, **no `asyncio.run` inside Alembic**):

```
bind = op.get_bind()
org_ids = [r[0] for r in bind.execute(sa.text("SELECT id FROM organizations")).fetchall()]
for org_id in org_ids:
    exists = bind.execute(sa.text(
        "SELECT 1 FROM account_types WHERE org_id=:o AND slug='loan' LIMIT 1"
    ), {"o": org_id}).first()
    if exists:
        continue  # existence-guard: there is NO UNIQUE(org_id, slug); a naive insert double-creates
    bind.execute(sa.text(
        "INSERT INTO account_types (org_id, name, slug, is_system) "
        "VALUES (:o, 'Loan', 'loan', TRUE)"
    ), {"o": org_id})
```

- Guard on **slug only** (not `is_system`), so an org that hand-created a custom `loan` type (is_system=False) is not double-seeded by the migration. Note a divergence: `seed_org_defaults` guards on the `is_system=True` slug set (`org_bootstrap_service.py:71-78`), so on a future **reset** a custom-loan org could receive a second, system `loan` type. Pre-launch this is acceptable and left as-is (aligning `seed_org_defaults` to a slug-only guard is out of scope — it would change behavior for every system type); document the divergence in the migration rather than claiming the guards match.
- **Downgrade:** drop the 5 columns; delete `account_types` rows with `slug='loan' AND is_system=TRUE` **that have no referencing `accounts`** (safe: never orphan an account's FK).

### 3.3 Validation — `backend/app/services/loan_service.py`

New `validate_loan_fields(*, target_slug, principal_amount, interest_rate_apr, term_months, origination_date, first_payment_date)` raising `HTTPException(422)`, mirroring `credit_card_service.validate_credit_card_fields`:

- **Non-loan target:** all 5 loan columns must be NULL (symmetric guard; closes the mirror, exactly as CC 422s a non-CC row carrying CC cols).
- **Loan target:** all 5 required (non-null); `principal_amount > 0`; `interest_rate_apr` in `[0, 999.99]`; `term_months` in `[1, 480]`; `first_payment_date >= origination_date` (cross-field, service-level — Pydantic has no cross-field for this).

**Pydantic belt** (`schemas/account.py`, mirroring `close_day: Field(ge=1, le=31)` at :36 and CC `max_digits`/`decimal_places` at :64-71) — parse-time 422 before any service runs, and prevents a DB 500 / ZeroDivision:
- `principal_amount: Field(gt=0, max_digits=12, decimal_places=2)`
- `interest_rate_apr: Field(ge=0, le=Decimal("999.99"), max_digits=5, decimal_places=2)`
- `term_months: Field(ge=1, le=480)`
- `origination_date`, `first_payment_date`: `date`
- All Optional; the "required on loan" coupling lives in the service (schema can't see the target slug).

### 3.4 Router + schema wiring (`backend/app/routers/accounts.py`, `schemas/account.py`)

- **Create path** (`create_account`, ~:111-141): add `validate_loan_fields(target_slug=target_type.slug, ...)` alongside the CC validators; add the 5 loan fields to the `kwargs` dict (~:153-168). Without both, loan create silently drops the fields.
- **Update path:** loan columns apply in `_apply_non_type_fields` gated on `model_fields_set` (mirror CC metadata columns), validated against `resolved_slug` (already threaded: fast path :278, atomic path :508). Add `validate_loan_fields` to that validation block.
- **Router gate** `touches_type_or_cc_columns` (:241-250): add the 5 loan fields, so a PUT touching only e.g. `principal_amount` takes the atomic/row-locked path (rename the local to `touches_type_or_liability_columns` for accuracy).
- **`_to_response`** (:53-77): add a nested `loan` object (see 3.6) — computed, no DB. Exposed on both `GET /accounts` (list) and `GET /accounts/{id}`; the row-detail UI needs it in the list.
- **`AccountResponse` / `AccountCreate` / `AccountUpdate`** (`schemas/account.py`): add the 5 input fields + the nested `LoanMetrics` output object.

### 3.5 Type-change cascade restructure — `account_type_change_service.py:447-504` (**the load-bearing regression fix; ships in Slice 1**)

Today the cascade is binary `if target_slug == _CC: … else: …`. With loan as a second liability, restructure into a per-slug matrix. Define `_LOAN = "loan"` and `_LIABILITY = {_CC, _LOAN}`.

Correct behavior per transition (verified against the unmodified `:447-504`):

| Transition | Required action |
|---|---|
| `asset → loan` | set loan cols (via `_apply_non_type_fields`); **keep** `payment_source_account_id`; clear CC cols (already null) |
| `CC → loan` | clear CC cols + delete `cc_cycle_payments`; **keep** `payment_source_account_id`; set loan cols |
| `loan → CC` | set CC cols; **clear the 5 loan cols**; keep `payment_source_account_id` |
| `loan → asset` | clear the 5 loan cols; clear `payment_source_account_id` |
| `loan → loan` | keep everything (idempotent) |
| `CC → asset` | unchanged from today |

Restructured logic:
1. **`payment_source_account_id`**: move its clear (today unconditional in the `else`, :473) behind `if target_slug not in _LIABILITY: account.payment_source_account_id = None`. *(This directly answers the review question: "clear on leave-CC" becomes "clear on leave-all-liabilities.")*
2. **CC columns + `cc_cycle_payments` delete** (:479-504): fire when `target_slug != _CC` (i.e. keep them on the loan branch too — a CC→loan must still shed CC data).
3. **Loan columns** (new symmetric clear): `if target_slug != _LOAN: account.principal_amount = account.interest_rate_apr = account.term_months = account.origination_date = account.first_payment_date = None`. Fires on `loan → CC` (which the old CC branch never cleared) and `loan → asset`.
4. **close_day / payment_day** stay exactly as today (CC-only).

No new required-field-on-enter payload gymnastics like close_day: loan columns are optional metadata applied via `_apply_non_type_fields` (like CC's `credit_limit`/`apr`), and `validate_loan_fields` enforces "loan target ⇒ all 5 present" on the resolved slug.

### 3.6 Computed metrics — `loan_service.py` (pure math, no storage)

Exposed as a nested `loan` object on `AccountResponse` (only populated when `slug == "loan"` and the 5 fields are present; else `None`). Monthly rate `r = interest_rate_apr / 100 / 12` computed on `Decimal`.

| Metric | Formula / rule |
|---|---|
| `expected_monthly_payment` (PMT) | `r == 0 → principal_amount / term_months`; else `P · r(1+r)^n / ((1+r)^n − 1)`, `n = term_months`, `P = principal_amount`. Rounded to cents (ROUND_HALF_UP). |
| `maturation_date` | `first_payment_date + relativedelta(months=term_months − 1)` (dateutil, the codebase idiom; clamps day-of-month). |
| `projected_payoff` | Solve remaining months from the **live** owed balance `B = −account.balance` (owed positive). **`B ≤ 0`** (paid off / overpaid, incl. a mid-life import already at zero) → `status="paid_off"`, `projected_payoff_date = today`, `n_rem = 0`. Else `r == 0 → n_rem = ceil(B / PMT)`; else if `PMT ≤ r·B` → `projected_payoff_date = null` + `status="interest_only"` (never amortizes; don't emit a bogus date); else `n_rem = ceil( −ln(1 − r·B/PMT) / ln(1+r) )`. **Anchor to the next scheduled payment date** (O1): `next_payment_date = first_payment_date + relativedelta(months=k)` for the smallest k with that date ≥ today; `projected_payoff_date = next_payment_date + relativedelta(months = n_rem − 1)` (compute the `B ≤ 0`/`n_rem == 0` branch BEFORE anchoring so you never do `next + (−1)`). Payment-date anchoring is deterministic (doesn't drift with the day the page is opened) and consistent with `maturation_date`. |
| `total_interest` | `PMT · term_months − principal_amount` (contractual, over the **full original term**; approximate after cent-rounding — the final scheduled payment absorbs the residual; documented). Note this reads high next to a partly-paid live balance on a mid-life loan — label it "Total interest (full term)" in the UI. |

No amortization schedule is materialized in V1, so the largest-remainder apportionment machinery is **not** used (note the tension so nobody wires up a schedule prematurely). `Decimal` for money + `ROUND_HALF_UP` for cents. **One acknowledged float hop:** the payoff `n_rem` solve uses `math.log`, forcing a Decimal→float step; `ceil` absorbs the float noise except at an exact-integer month boundary where it could flip ±1 month — acceptable for a projection, not a stored balance. Everything else stays `Decimal`.

### 3.7 Frontend (`frontend/app/accounts/page.tsx`, combined inline page)

- **De-gate the payment-source picker** from CC-only to CC-or-loan: the create gate `selectedType?.slug === "credit_card"` (~:983) and the edit gate `editingTypeSlug === "credit_card"` (~:1189) → `=== "credit_card" || === "loan"`. Update the adjacent "credit-card-only" comments. *(Line numbers are approximate — `accounts/page.tsx` has ~15 `credit_card`-gated blocks; locate the payment-source picker specifically.)* `paymentSourceOptions` already excludes self + lists checking/savings/cash; `payment_source_service` gates only the *source* type, so loan targets need **zero backend change**.
- **Loan-gated form fields** (create + edit), shown when the selected/edited slug is `loan`: principal amount, interest rate (%), term (months), origination date, first payment date, payment-source picker.
- **Read-only loan subline on the account row** (parallel to CC's utilization subline, ~:1422-1455 — approximate): expected monthly payment; **"Matures <date>"** (contractual) shown distinctly from **"On track to pay off <date>"** (live-solved) so the two dates never read as the same thing; **"Total interest (full term)"**; "paid from <source>". When `status="interest_only"` show a quiet **"Not on track to pay off"** (DESIGN quiet-by-default tone; never a fabricated date, never the bare phrase "interest-only" which names a real mortgage product); when `status="paid_off"` show "Paid off". No amortization table in V1. Confirm final copy with the operator during the UI pass.
- **Dashboard:** none needed — `BalancesByTypeTile` groups by `account_type_id` and already lists `loan` in `SLUG_ORDER` + icon; loans appear automatically.
- **Types:** add the 5 fields + `LoanMetrics` to `Account` in `lib/types.ts`.

### 3.8 Slice 1 tests

- Migration up/down/up on **MySQL** (not just SQLite CI): columns present, backfill inserts loan type once per org, re-run idempotent, downgrade drops cols + loan type (only where unreferenced).
- `validate_loan_fields`: each range 422; loan-target-missing-field 422; non-loan-carrying-loan-cols 422; `first_payment_date < origination_date` 422; boundary values (term 1 and 480, apr 0).
- Type-change matrix: **all 6 transitions** in the table above assert the exact column state after change (esp. CC→loan keeps `payment_source_account_id`; loan→CC and loan→asset clear loan cols; loan→loan idempotent).
- Metrics: PMT vs known amortization fixtures; `r==0 → P/n`; maturation date; payoff solve incl. `interest_only` and already-paid (`balance == 0` → payoff now / 0 months); total_interest.
- Create + PUT wiring: loan create persists all 5; PUT of a single loan field takes the atomic path and validates.

## 4. Slice 2 — forecast synthesis (`account_balance_forecast_service.py`)

New `loan_forecast_service.py` mirroring `cc_forecast_service.py`, wired into `account_balance_forecast_service` (per-account; includes transfer legs) — **not** `forecast_service` (uses `reportable_transaction_filter()`, excludes transfer legs and has no per-account balance → would *miss* loan payments, not double-count them; brief wording corrected).

- **Loan filter/gate** (separate from CC, not a widened CC filter): `slug == "loan"` AND `payment_source_account_id is not None` AND all 5 loan fields present AND `source.currency == loan.currency` (skip on FX; no FX in V1).
- **Synthesis** — CC-clone, conserving. For each scheduled payment date in the horizon (`first_payment_date + relativedelta(months=k)` for k ≥ 0 falling in window, up to `term_months` payments): `outstanding = max(0, −loan.balance_as_of_date)`; `applied = min(PMT, outstanding)`; `source.expected -= applied`; `loan.expected += applied`. `Σ synth == 0`.
- **Cap source:** whether `outstanding` is `−account.balance` (current) or a per-date ledger reconstruction — **choose current-balance for V1** (simpler; no ledger replay). If a per-date reconstruction is ever needed to thread multiple payments in one horizon, use `balance_contribution_filter()` (`transaction_filters.py`) per the CC Slice 3 lesson, never `reportable_`/`non_reverted_`. Thread `applied` forward across a multi-payment horizon like CC threads `S_prev` so a second in-window payment sees the reduced outstanding.
- **Already-paid-this-period guard** (Important — prevents projecting a phantom payment on top of one the user already recorded). **The two spec reviewers disagreed on the mechanism; resolve with a tie-break review at the start of Slice 2 (does not affect Slice 1).**
  - *Period-skip (leaning recommendation):* because loan uses **current-balance** outstanding (`−account.balance`, which ALREADY reflects any recorded payment), the correct action is to **skip** a period's synthesis when a settled source→loan transfer exists in that period's window (`prev_scheduled_payment_date < eff ≤ this_payment_date`). Netting the recorded amount out (CC's `p_k_owned` style) would **double-subtract** here, since the current balance already dropped.
  - *p_k_owned clone (dissent):* clone CC's credit-attribution against credits landing on the loan account, threading `applied` forward. This is correct only under an as-of-date balance that excludes the payment — which is NOT the current-balance model chosen. Recorded here for the tie-break.
  - **Shared, accepted limitation (both agree):** a payment recorded as a bare expense on the source with no transfer leg to the loan leaves `loan.balance` unmoved and is undetectable → possible phantom double-drop. This is identical to CC's existing dependence on linked transfer legs; consistent, not a new defect. Make this transfer-shape assumption explicit in the Slice 2 code + docstring.
- **Cash-basis** via `effective_period_date_expr`.
- **Optional** month-end tile line ("loan payment $X on <date>"): add a `loan_payments` array to the forecast payload analogous to `cc_payments` (`account_balance_forecast_service.py:209`). Not required for Slice 2 to be useful.

### 4.1 Slice 2 tests
- Single loan payment in horizon drops source by PMT and moves loan toward zero; `Σ synth == 0` (per-account expected sums to currency total).
- `outstanding == 0` (paid off) → no-op.
- Two payment dates in one horizon → second sees reduced outstanding (threading).
- Already-recorded payment in period → no phantom double-drop.
- FX mismatch (`source.currency != loan.currency`) → skipped.
- `forecast_service` (reportable) unaffected — no loan synthesis leaks into reportable aggregates.

## 5. Out of scope V1 (unchanged from the 2026-05-15 spec)

Per-payment principal/interest split UI; full amortization schedule UI; ARM/variable-rate; extra-principal "what-if" simulator; auto disbursement transaction; auto recurring template; refinancing; multi-disbursement lines of credit; interest tax tracking.

## 6. Follow-ups (own PRs, after V1)

- **Disbursement transaction** (one-click, optional, checkbox default ON): transfer-pair (`linked_transaction_id`) crediting the source asset by `principal_amount`, debiting the loan.
- **Auto recurring template**: monthly from `first_payment_date`, amount = PMT, source = `payment_source_account_id`, category "Debt Payment"; update hook on source change.
- **V2:** amortization-aware forecast + per-payment split (makes the true hybrid sound); amortization schedule UI; reports surfacing.

## 7. Open notes — status after two APPROVE sign-offs

- **O1 — payoff anchor: RESOLVED.** Anchor to the next scheduled payment date: `projected_payoff_date = next_payment_date + relativedelta(months = n_rem − 1)`, `next_payment_date = first_payment_date + relativedelta(months=k)` for smallest k with date ≥ today; `B ≤ 0` / `n_rem == 0` short-circuits to `paid_off`/today BEFORE anchoring. Folded into §3.6.
- **O2 — already-paid heuristic (Slice 2 only): OPEN, tie-break at Slice 2 start.** Reviewers split between period-skip and a `p_k_owned` clone; leaning period-skip for the current-balance model (see §4). Does not block Slice 1.
- **O3 — `interest_only` / `paid_off` surfacing: RESOLVED.** Quiet status, null date, no bare "interest-only" phrasing; copy "Not on track to pay off" / "Paid off". Folded into §3.7; confirm final wording with the operator during the UI pass.

## 7b. Accounts page redesign (bundled into this PR at operator request)

The Slice-1 UI stacked CC utilization + loan metrics into the balance table cell, making liability rows ~5 lines tall (spreadsheet-skin, the PRODUCT.md anti-reference). Operator asked to restore a clean balance list and move CC + Loan detail into dedicated cards on the same page. Two grounded reviews (architect: feasibility/data; design critic: on-brand/composition) both returned CHANGES REQUIRED on the assessment; all folded. Final:

- **Zone 1 — clean table:** strip the CC-utilization + loan sublines from the balance cell (keep balance / Pending / Opening); drop the inline "· closes day · paid from" subtitle from the row name column. Rows are one line again.
- **Zone 2 — liability cards** (`frontend/components/accounts/LiabilityCards.tsx`), full-width sibling BELOW the two-panel grid (not nested inside the Accounts card): grouped "Credit cards" / "Loans", **2-column max** grid (not auto-fit — the AI-card-wall tell). Each card = account-name label → **balance hero** (sans, tabular-nums) → **one expressive element** (CC: reused `CreditUtilizationBar` with new `hideName`; Loan: payoff status chip via `badge*`) → hairline divider → **borderless** 2-col label/value metric list. Accent rides status tokens only (gold reserved, One Brass Rule). Metric labels use `text-secondary` (not `text-muted`, which fails AA on the light theme).
- **Data:** zero new endpoint — every field is already on the account object / nested `account.loan` / client-derived. Cards read the **unpaged** `sortedAccounts` (a liability on page 2 keeps its card). Shared `resolvePaymentSource(accounts, id)` helper.
- **Copy:** month-year dates ("Matures Jun 2054", "On track · paid off by Nov 2030"); `interest_only` → "Payment covers interest only"; `paid_off` → "Paid off"; teachable empty cards ("No credit limit set" / "Finish setting up this loan"). Ordering: CCs by utilization desc, loans by balance magnitude desc. Inactive liabilities shown dimmed (matches the table).
- **Tests:** the two loan/CC row-subline test blocks + one payment-source assertion re-pointed at the new card testids with the new copy; three accounts-page tests loosened `getByText(name)` → `getAllByText` (names now legitimately appear in row + card); new `liability-cards.test.tsx` (helpers, grouping, ordering, teachable state). `formatMonthYear` added to `lib/format.ts`.
- Visual-verified on the live stack (light theme). Frontend: 322 files / 2615 tests, tsc + eslint + design-tokens clean.

## 8. Sign-off record

- 2026-07-24 — two independent grounded design reviews (schema/validation + forecast/product) → all Critical/Important findings folded into this spec.
- 2026-07-24 — two independent grounded **spec** sign-offs → both **APPROVE**, low-severity polish only, all folded above. Cleared to implement Slice 1.
- Note: `demo_seed_service.py` creates no loan account; not required for V1 (flagged so nobody expects a loan in seeded data). No `wipe_org_data`/`reset_org_data` change needed (columns ride on `accounts`; no new hard-FK child table like `cc_cycle_payments`).
</content>
