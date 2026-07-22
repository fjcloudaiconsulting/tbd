---
name: Credit Card Model V1 (full, incl. forecast integration)
description: 2026-07-22 design. Ships the remaining CC fields from the 2026-05-15 upgrade spec (credit_limit, apr, payment_strategy, fixed_payment_amount) PLUS carried-balance forecast integration and a per-cycle payment-amount store for minimum_only/custom_per_period. Owner-decided the heavier options; architect-reviewed (forecast, schema, UX).
type: project
---

# Credit Card Model V1 (full, incl. forecast integration)

**Date:** 2026-07-22. **Status:** design, pre-implementation. Owner decisions locked in this session; three architect passes folded in (forecast integration, schema/validation, UX/display).

**Builds on (already shipped, do NOT redesign):**
- Per-CC cycle resolver `backend/app/services/cc_cycle_service.py` (Slice 1, merged 2026-05-29): `resolve_cycle_for_account(account, target_date) -> CreditCardCycle(period_start, period_end_inclusive, payment_date, source)`. `close_day`, `payment_day`, `payment_day_relative_month` columns exist. All cycle math lives here; callers must not re-derive (D8 of `specs/2026-05-28-cc-billing-cycle.md`).
- Payment Source Foundation (#565, merged 2026-07-22): `accounts.payment_source_account_id` self-FK (ON DELETE SET NULL, migration 072), validated to an active same-org checking/savings/cash account. `payment_source_service.py`. Leave-CC cascade already clears it.

**Owner principle (locked, do NOT violate):** PFV is a personal-decision planning tool, not a bank/billing system. No cron mutations of financial data, no computed interest, no computed minimum payment. Statement/outstanding balance is a COMPUTED READ, never a stored/mutated snapshot.

## Owner decisions (this session)

1. **Scope = full V1 including forecast integration** (not fields-and-display only).
2. **Statement balance = carried balance.** The projected `full_balance` payment reflects TOTAL outstanding owed at cycle close (this cycle's charges plus anything carried), not one cycle's net spend.
3. **`minimum_only` / `custom_per_period` get a real per-cycle amount-entry surface**, so the forecast can project an exact outflow for those strategies.
4. **`credit_limit` is optional + non-enforcing.** Not required to create a CC; editable anytime; the user MAY overspend it. Over-limit shows the real state; nothing blocks or clamps.

## Schema

### Migration 073 (`073_credit_card_model_v1`, down_revision `072_payment_source_account_id`)

Single additive `ALTER TABLE accounts`, four nullable columns (all NULL on non-CC rows, mirroring the `close_day` fat-row invariant; no `server_default`):

| Field | Type | Notes |
|---|---|---|
| `credit_limit` | `Numeric(12,2)` NULL | Optional. `> 0` if provided. Non-enforcing (no `balance <= limit` check anywhere). |
| `apr` | `Numeric(12,2)` NULL | Optional metadata, no computed use in V1. Stored as a **percent** (e.g. `19.99`), range `[0, 100]`. |
| `fixed_payment_amount` | `Numeric(12,2)` NULL | Required iff `payment_strategy == fixed_amount`. |
| `payment_strategy` | native MySQL `ENUM` NULL | `{full_balance, minimum_only, fixed_amount, custom_per_period}`. NULL-at-rest; resolver treats NULL as `full_balance`. |

`payment_strategy` is a native MySQL ENUM (a genuinely CLOSED set of 4, so the ABN `.TAB` ENUM-growth landmine does not apply — that rule targets growth axes like `segment`/`source_format`). Python enum stores lowercase via `values_callable=lambda x: [e.value for e in x]`, `name="account_payment_strategy"`:

```python
class PaymentStrategy(str, enum.Enum):
    FULL_BALANCE = "full_balance"
    MINIMUM_ONLY = "minimum_only"
    FIXED_AMOUNT = "fixed_amount"
    CUSTOM_PER_PERIOD = "custom_per_period"
```

Migration passes raw value tuples (`sa.Enum(*_STRATEGIES, name="account_payment_strategy")`), NOT the Python enum, so it doesn't import app models (matches shipped idiom in `045_reconciliation_state.py`). **Verify with `alembic upgrade head` against a MySQL container before merge — SQLite CI cannot catch native-ENUM DDL drift.**

### Migration 074 (`074_cc_cycle_payments`, down_revision `073_credit_card_model_v1`)

New dedicated table for per-cycle payment amounts. NOT an extension of `cc_cycle_overrides` — that table was only ever a spec proposal (D7) and never shipped; only the DB-free resolver did. A dedicated table keeps `amount` `NOT NULL` (no CHECK needed) and depends only on the shipped resolver.

```python
op.create_table(
    "cc_cycle_payments",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("account_id", sa.Integer(), nullable=False),
    sa.Column("period_anchor_year", sa.SmallInteger(), nullable=False),
    sa.Column("period_anchor_month", sa.SmallInteger(), nullable=False),
    sa.Column("amount", sa.Numeric(12, 2), nullable=False),
    sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
    sa.UniqueConstraint("account_id", "period_anchor_year", "period_anchor_month",
                        name="uq_cc_cycle_payments_account_period"),
)
```

- **Anchor = the cycle's CLOSE month** (same anchoring as the D7 override proposal). A Jan-25 close paid Feb-1 stores under `(account, 2026, 1)`.
- **No `org_id` column** — org isolation is enforced at the router by loading the parent account under `current_user.org_id` (universal `accounts.py` pattern). `ON DELETE CASCADE` because a payment row is meaningless without its account.
- New model `CcCyclePayment` in `backend/app/models/cc_cycle_payment.py`. Must join both `wipe_org_data` and `reset_org_data` before its parent account (org-delete cascade FK audit rule) — though CASCADE via `account_id` makes it automatic; confirm during implementation.

## Validation — `backend/app/services/credit_card_service.py`

New single-purpose service (mirrors `payment_source_service.py`), raises `HTTPException(422)`, returns `None`.

`validate_credit_card_fields(*, target_slug, credit_limit, apr, payment_strategy, fixed_payment_amount)`:
- Non-CC target: all four CC-only columns must be NULL, else 422 `"<field> is only allowed on credit_card accounts"`.
- CC target:
  - `credit_limit` optional; if provided must be `> 0`.
  - `apr` if provided in `[0, 100]`.
  - `fixed_payment_amount` required and `> 0` iff `payment_strategy == fixed_amount`; forbidden otherwise.
- No `require_credit_limit` parameter (owner made limit optional). No `balance <= credit_limit` check anywhere.

Deliberate status divergence: these new rules use **422** (matching `payment_source_service`); the older `close_day` rules use 400. Accepted inconsistency; do not move CC-field rules into `account_type_change_service` to unify.

Per-cycle payment validation (`validate_cycle_payment`, in `cc_cycle_service.py` or a thin `cc_cycle_payment_service.py`):
- `amount > 0` → 422.
- Anchor must be **current-or-future**; past-cycle write → **409** (matches D6 read-only-past + the Slice-3 locked test plan). Judge via `resolve_cycle_for_account(account, today)`.
- The `(account, year, month)` must resolve to a real CC cycle (account is `credit_card` with non-NULL `close_day`); reuse the shipped resolver, do not re-derive.
- **Gate on `slug == 'credit_card'` ONLY, NOT on `payment_strategy`.** Store amounts regardless of current strategy (strategy is mutable/NULL-at-rest; gating writes on it strands data). The forecast READER decides at read time whether to consult the table based on the strategy then in effect. Non-CC accounts rejected (422).

## Router wiring

### `accounts.py` (existing)
- `AccountCreate` / `AccountResponse` gain the four fields; `_to_response` exposes them.
- Create path: call `validate_credit_card_fields` after `validate_create_payment_day`; add fields to insert kwargs.
- PUT path: extend `touches_type_or_cc_columns` to include the four new `model_fields_set` keys so any CC-field edit routes through the atomic path; call `validate_credit_card_fields` inside `_apply_non_type_fields` with the POST-change slug and the post-lock row snapshot overlaid with payload deltas (validate resulting state, not just the payload).

### **GOTCHA — leave-CC cascade (mandatory)**
`apply_type_change_in_session` else-branch (`account_type_change_service.py`) currently clears `close_day`, `payment_day`, `payment_day_relative_month`, `payment_source_account_id`. The four new CC-only columns MUST join this clear, or they orphan on an asset row (a Checking account silently retaining a `credit_limit`/`payment_strategy` no UI can surface). Add:
```python
account.credit_limit = None
account.apr = None
account.payment_strategy = None
account.fixed_payment_amount = None
```
Regression test required (same bug class as the one HIGH finding on #565). `cc_cycle_payments` rows are NOT cleared on type change — they CASCADE on account delete, and a card that leaves CC simply stops being read by the forecast; a later revert reuses them. (Confirm this is acceptable during review; alternative is to delete them on leave-CC.)

### New router — `cc_cycle_payments.py` (or a section of `accounts.py`)
- `GET  /api/v1/accounts/{account_id}/cycle-payments` — collection feeding the UI mini-list; returns upcoming N cycles with `{year, month, close_date, due_date, amount|null}` (dates from the shipped resolver so the FE never re-derives cycle math).
- `POST` / `PUT` / `DELETE /api/v1/accounts/{account_id}/cycle-payments/{year}/{month}` — body `{ "amount": Decimal }`.
- Org-scoped + **owner/admin only** (`_is_admin_user`). `{year}/{month}` = the close-month anchor.

## Forecast integration — `account_balance_forecast_service.py`

The CC payment is synthesized in **`account_balance_forecast_service`** (per-account month-end balances, which deliberately INCLUDES transfer legs), **NOT** `forecast_service` (the reportable income/expense aggregate, which excludes transfers by `reportable_transaction_filter` invariant — injecting there would double-count and break parity with the dashboard donut).

Representation: an **ephemeral in-memory delta** with provenance `source="credit_card_payment"`. NOT a persisted transfer pair, NOT an auto-recurring row (a recurring row drifts every cycle for `full_balance` and needs auto-update — rejected). Pure read, always correct.

### Cycle enumeration (per active CC with non-NULL `payment_source_account_id`)
Walk cycles across the open-period horizon `[p_start, p_end]` via `resolve_cycle_for_account`; for each cycle whose `payment_date` falls in the horizon, synthesize one outflow on that `payment_date`. Thread `S_prev` (sum of earlier synthesized outflows this horizon) so a horizon spanning two due dates doesn't double-bill.

### Amount resolution (unified, all four strategies)
```python
def resolve_cc_payment_amount(*, account, cycle, balance_at_close, recorded_post_close_credits,
                              prior_synth_credit, per_cycle_amounts) -> Decimal:
    s = account.payment_strategy or "full_balance"           # NULL-at-rest default
    if s == "full_balance":
        return max(Decimal("0"),
                   -balance_at_close - recorded_post_close_credits - prior_synth_credit)
    if s == "fixed_amount":
        return account.fixed_payment_amount or Decimal("0")   # literal
    # minimum_only + custom_per_period: same per-cycle store lookup
    anchor = (account.id, cycle.period_end_inclusive.year, cycle.period_end_inclusive.month)
    return per_cycle_amounts.get(anchor, Decimal("0"))        # unset => project nothing
```

- `balance_at_close` (`B_k`) = `account.balance` + Σ pending deltas with `eff_date <= close_date` (`eff_date = coalesce(settled_date, date)`; signed income+/expense−; transfer legs included). Owed is stored NEGATIVE (`balance = opening_balance + Σ settled(income−expense)`), so `-B_k` is the positive outstanding magnitude and includes carried debt automatically — this IS the carried-balance semantic.
- `recorded_post_close_credits` (`P_k`) = Σ CC payment-in credits (transfer legs, `linked_transaction_id` not null, income) with `eff_date` in `(close_date, payment_date]` — so a real payment recorded before the due date reduces (never double-counts) the synthesized amount.
- Apply as two deltas: `source.expected -= amount`, `cc.expected += amount` (source asset drops; CC liability moves toward zero). Batch-fetch `per_cycle_amounts` once (avoid per-cycle queries).
- Statement window for `B_k`/`P_k` uses `effective_period_date_expr()` (cash-basis, inclusive close per D2).

### Edge cases
- `payment_source_account_id` NULL → no synthesis (user models manually).
- Source account inactive → the projection only iterates active accounts; treat as no-op (do not resurrect an inactive source).
- Card in credit (`B_k >= 0`) → `outflow = 0`.
- `minimum_only`/`custom` with no stored amount for the anchor → project nothing; UI shows quiet "amount not set".
- Horizon shorter than one cycle / zero due-dates in horizon → no outflow.

## UX / display (design-token-clean, no em-dashes, quiet-by-default)

**CC balance sign (load-bearing):** liabilities are NEGATIVE balances. A card owing 1,200 renders `-1,200.00` today (`formatAmount` preserves sign). All formulas below use that convention.

### Utilization / available-credit subline (on the `/accounts` liability row)
Quiet muted subline (`text-xs tabular-nums text-text-muted`, no accent, no status token), in the idiom of the existing "Pending:" / "Opening:" sublines. Render only when `slug == credit_card` AND `credit_limit > 0` (else stay silent — no "—"). No dashboard CC tile in V1 (that is a later slice).

- `outstanding = max(0, -bal)`; `utilization% = round(outstanding / limit * 100)` (UNCAPPED); `available = limit + bal`; `over = outstanding - limit` when positive.
- Copy by case:
  - `outstanding == 0`: `0% used · full limit available`
  - within limit: `Using {n}% of limit · {available} {currency} left`
  - over limit: `Using {n}% of limit · {over} {currency} over`  (positive "over" magnitude, never a negative "left")
- **No color band in V1, even over-limit.** Over-limit is an owner-permitted state, not an error PFV discovered; the balance sign already carries the "you owe" signal. Threshold coloring (`badgeWarning`/`badgeError` via the `chartColor.watch/over` precedent) is a deferred owner decision.

### CC form fields (extend the existing `slug == 'credit_card'` gated block)
Order: Bill close day (exists) → Credit limit → APR → Paid from (exists) → Payment strategy → Fixed payment amount (conditional). Use `select`/`input`/`label` primitives from `lib/styles.ts` (SELECT for strategy — no radio primitive exists; adding one is out of scope). When `payment_strategy != fixed_amount`, clear/hide `fixed_payment_amount` (mirrors existing conditional-field clearing). `Account` type + create/PUT bodies in `frontend/lib/types.ts` / `accounts/page.tsx` gain the four fields.

### "Upcoming payments" per-cycle entry (fresh build; `cc_cycle_overrides` UI never shipped)
Inline expandable mini-list in the **edit-account** expanded block (not the collapsed row — avoids a spreadsheet-skin list), gated to `payment_strategy ∈ {minimum_only, custom_per_period}` (hidden for full_balance/fixed_amount). Shows next **N = 3** cycles from the GET collection: each row = cycle window (`Closes {close} · due {due}`) + amount `input` + `Clear` (shown when a saved amount exists). Persist on blur/Enter via `PUT …/{year}/{month}`; empty → `DELETE`. Cycle dates come from the backend (FE must not re-derive).

Copy: section `Upcoming payments`; helper `Enter what you plan to pay each cycle. We use it in your forecast.`; row `Closes {close_date} · due {due_date}`; placeholder `amount not set`; empty `No upcoming cycles yet. Set a bill close day first.`

### Forecast "Payment $X on due day" line
Quiet muted labeled line in the `AccountMonthEndForecast` idiom (`text-[10px] tabular-nums text-text-muted`, currency via existing `currencySymbol()`, no status color). Shows the resolved amount (`full_balance`/`fixed_amount` always; `minimum_only`/`custom` once a per-cycle amount is entered). For an unfilled `minimum_only`/`custom` cycle: `Payment amount not set` (muted, no color). Exact placement (this card vs forecast-plans detail) is an implementation call; the visual idiom is fixed.

### APR framing
Optional labeled input only (`APR (%)`). No gauge, no "high-APR" warning, no computed output anywhere in V1.

## Audit
- New CC metadata fields on create/PUT: **no new audit events** (matches the `payment_source_account_id` precedent — only opening-balance and type-change are audited today). Optionally extend the existing `account.type_changed` detail with `credit_limit_cleared` etc. keys for the leave-CC clear (optional polish).
- Per-cycle payment writes ARE money-bearing → emit `account.cycle_payment.created` / `.updated` / `.deleted` via `audit_service.record_audit_event` (actor snapshot, `target_org_id`, `detail={account_id, year, month, amount}`, old+new on update), fired post-commit (mirrors `account.opening_balance.update`).

## Out of scope (reaffirmed)
Interest accrual, computed minimum payment, statement-closing transaction generation, cron of any kind, frozen statement snapshots, per-CC cycle date-overrides UI (Slice 3, still unbuilt — this store does not depend on it), dashboard CC tile, utilization threshold coloring.

## Sequencing / PRs
Per the architect small-PR principle, candidate slices (may bundle if review stays small):
1. **073 schema + validation + form fields + utilization subline** (fields & display; `credit_limit`/`apr`/`payment_strategy`/`fixed_payment_amount`, leave-CC cascade, `credit_card_service`).
2. **074 store + endpoint + audit + "Upcoming payments" UI** (per-cycle amounts).
3. **Forecast integration** in `account_balance_forecast_service` (carried-balance synthesis, unified resolver, provenance).

Each PR: auto-dispatch the review team, fold ALL findings before merge; `main` needs 1 human review (agent reviews are not GitHub approvals). No AI attribution in commits/PRs.

## Open flags for the owner (call out at review)
1. `cc_cycle_overrides` (Slice 3, date-overrides UI) is still unbuilt; this store is independent of it, but the CC "detail page" that would host both sections does not exist — the per-cycle UI lives inline in the edit block instead.
2. Confirm leave-CC does NOT delete `cc_cycle_payments` rows (they persist, unread, and CASCADE on account delete). Alternative: delete on leave-CC.
3. Run `alembic heads` to confirm 072 is the sole head before adding 073/074.
4. Deferred: utilization threshold coloring (incl. whether over-limit earns `badgeError`); "high-APR" hint.

## Cross-references
- `specs/credit-card-model-upgrade.md` (2026-05-15) — the field-level source spec this ships.
- `specs/2026-05-28-cc-billing-cycle.md` — cycle substrate (Slice 1 shipped; D2/D6/D7 anchor the store shape and past-read-only rule).
- `specs/payment-source-account-foundation.md` + memory `reference_payment_source_foundation` — the shipped foundation this consumes.
- `reference_effective_period_date_cash_basis` — cash-basis bucketing for `B_k`/`P_k`.
