---
name: Credit Card Billing Cycle — Discovery + Substrate + Overrides
description: 2026-05-28 spec following the 2026-05-27 owner backlog. Reconciles the close-day-is-unused discovery finding with the existing 2026-05-15 CC model upgrade spec, locks the architect decisions, and proposes the per-CC cycle substrate that the user-visible overrides feature requires.
type: project
---

# Credit Card Billing Cycle — Discovery + Substrate + Overrides

**Date:** 2026-05-28. **Status:** RECONCILED 2026-07-23 — see the reconciliation section directly below before reading the rest.
**Replaces:** nothing (extends `specs/credit-card-model-upgrade.md` from 2026-05-15).
**Linked memory:** `project_credit_card_billing_backlog.md` (the 2026-05-27 owner backlog this responds to).

## 2026-07-23 Reconciliation & Architect Verdict (READ FIRST — supersedes the sequencing below)

Between 2026-05-28 and 2026-07-23 a large amount of CC work shipped that this spec could not
foresee: the per-CC cycle resolver (Slice 1), Credit Card Model V1 (`credit_limit`, `apr`,
`payment_strategy`, `fixed_payment_amount`, and the per-cycle **amount** store `cc_cycle_payments`),
the payment-source foundation, and CC statement alerts. Two independent architects re-validated D1-D8
against today's codebase (2026-07-23) and the owner confirmed their verdict. Net result:

**Slice-by-slice status after reconciliation:**

- **Slice 1 (substrate) — SHIPPED & mature.** `backend/app/services/cc_cycle_service.py` exposes the
  pure resolver `resolve_cycle(...) -> CreditCardCycle(period_start, period_end_inclusive,
  payment_date, source)`; `source` reserves `"override"` for the (now deferred) overrides work.
  Columns `accounts.payment_day` + `accounts.payment_day_relative_month` exist. Consumed by the
  forecast (`cc_forecast_service` / `account_balance_forecast_service`), both statement-alert jobs,
  and `cc_cycle_payment_service` anchor resolution.
- **Slice 2 (configurable payment day) — BACKEND SHIPPED; FRONTEND IS THIS DELIVERABLE.**
  `schemas/account.py` accepts `payment_day` (ge=1,le=31) + `payment_day_relative_month`
  (ge=0,le=12); `validate_create_payment_day` + `validate_payment_day_cascade` are wired into
  create + PUT. The only missing piece is the CC-gated form UI. **This is what ships now** (its own
  small PR): two CC-gated fields in the create form and inline editor of `frontend/app/accounts/page.tsx`.
  The two controls map to their two columns **independently** (the resolver defaults each column
  separately, so they must not be coupled — coupling them makes "1st of the same month" unreachable
  and lets a blank day silently override a "same month" choice):
    - "Payment day (1-28)" number input: blank ⇒ send `payment_day: null` (resolver default = day 1).
    - "Payment month" select: "Month after close" (default) ⇒ send `payment_day_relative_month: null`
      (resolver defaults it to 1 = month after close; keeps the column NULL-at-rest per D3/D7,
      matching the existing `payment_strategy` "default option → null" idiom in this form); "Same
      month as close" ⇒ send `0`.
  Every combination is reachable, including "1st of the same month" (day blank + same-month). No
  backend change.
- **Slices 3 (per-month date overrides) + 4 (current-month shortcut) — DEFERRED to backlog.** Rationale
  and the correct build-it-later design are in the "Deferred: Slices 3-4" section appended at the end
  of this doc. Build only if users actually ask for per-month **date** shifting.

**Why 3-4 are deferred (unanimous, independent architect judgment):** the high-value per-cycle axis —
*how much* you pay this cycle — is already solved by `cc_cycle_payments`, and forecast + both alert
jobs already work correctly off the default cycle. The date-override axis shifts a close/payment
boundary by ~1-2 days ("the 25th was a Saturday"); that is marginal planning accuracy against a large,
money-and-scheduler-adjacent blast radius. Shipping it *honestly* means overrides must flow through
the resolver into forecast + both alert jobs + `cc_cycle_payments` anchoring (display-only is a
correctness lie — rejected), which touches the DB-free forecast hot path and the alert dedup markers.
Not worth the complexity budget until demand is validated.

**Architect rulings that constrain any future 3-4 build (D1-D8 all still hold; these refine them):**

- **Propagation (was implicit in D5/D8):** overrides flow end-to-end into all resolver consumers, or
  the feature does not ship. No display-only tier.
- **Resolver architecture (refines D8):** keep `resolve_cycle` PURE. Add an optional injected
  `overrides` map param and a one-shot batch-fetcher, mirroring the proven `per_cycle_amounts`
  batch-fetch → pure `synthesize_account_cc_payments` pattern at
  `account_balance_forecast_service.py:130-135`. Do **not** add a per-call async DB fetch inside the
  resolver — it would be an N+1 in `cc_forecast_service.due_cycles_in_horizon` (up to 60 iterations)
  and break the module's deliberate DB-free contract. An async `resolve_cycle_with_overrides(db,
  account, target_date)` convenience is fine for genuinely single-shot callers only.
- **Storage (confirms D7):** keep `cc_cycle_overrides` as a SEPARATE table from `cc_cycle_payments`.
  Amounts are money-bearing (owner/admin gate, `account.cycle_payment.*` audit); date shifts are
  config-grade. Reuse the sibling's STRUCTURE (isolation-via-parent-load, past-anchor 409, explicit
  wipe/reset deletes, leave-CC cascade), not its row.
- **Anchor stability (resolves D-open):** anchor-month stability is STRUCTURAL — `_clamp_day` keeps a
  `close_day_override` inside its calendar month, so an override can never move `period_end_inclusive`'s
  month, and a committed `cc_cycle_payments` amount stays attached to its cycle. Cross-month shifting
  is neither needed nor representable in D7's schema. Lock it with a unit test; reject any future
  "close N days after X" axis that would break it.
- **Consumer-list correction:** `account_type_change_service` is NOT a resolver consumer — it only
  runs cascade validation and deletes `cc_cycle_payments`. For overrides it needs only the leave-CC
  CASCADE DELETE (+ `account.cycle_override.deleted` audit), not override-aware resolution.
- **Re-slice:** Slice 4's "current-month shortcut" is just a second entry point into Slice 3's
  endpoint (see line ~172), so 3+4 are ONE PR, not two. Slice 2 frontend is independent and lands first.

**Mandatory landmines for any 3-4 build (from the architect risk pass):**

1. **Alert dedup re-fires on a shifted close date.** Both jobs dedup on `(account_id,
   period_end_inclusive.isoformat())` (`scheduler/audit.py`, `cc_statement_{close,reminder}.py`). If a
   user records a close-day override AFTER an alert fired, the resolved close date moves → new dedup
   key → the same cycle re-alerts (in-app + email spam). Any override-aware alert path MUST re-key
   dedup on the stable anchor MONTH `(account_id, anchor_year, anchor_month)`.
2. **Forecast carried-balance perturbs adjacent cycles.** `synthesize_account_cc_payments` threads
   `S_prev` and a consumed-credit set across all due cycles; an override that shifts one cycle's
   close/payment date mid-horizon silently re-nets neighbors. Needs targeted tests before shipping.
3. **Migrate forecast AND the cycle-payments "upcoming" list atomically** — if overrides flow into one
   reader but not the other, the amount editor and forecast disagree on a cycle's due date.
4. **New table's cascade obligations:** `cc_cycle_overrides` must be deleted in BOTH `wipe_org_data`
   AND `reset_org_data`, plus the leave-CC cascade in `account_type_change_service` — per
   `reference_org_delete_cascade_fk_audit`.
5. **Alembic revision-id must fit `VARCHAR(32)`** (SQLite CI passes a longer id; prod MySQL 500s).
6. **D6 refinement for close overrides:** "current" is computed from the DEFAULT cycle; reject a
   close-day override whose effective close date is already `< today` (stricter than the amount
   past-anchor 409 — editing an already-closed cycle's close date breaks reconciliation + re-fires
   alerts). Payment-date-only overrides keep the amount-style current+future rule.

## TL;DR

The 2026-05-27 owner backlog asked us to add a configurable Payment day, per-month overrides for close + payment days, and a current-month edit shortcut for credit-card accounts. Discovery shows the substrate those features ride on does not exist:

- `accounts.close_day` is stored but **never read** by billing logic.
- `BillingPeriod` is org-wide (one row per org per period, anchored on `Organization.billing_cycle_day`); there is no per-account or per-credit-card cycle.
- Transactions carry no `billing_period_id`; cycle bucketing is a date-range query at read time.
- No payment date is stored or computed anywhere; the "implicit 1st of next month" the owner backlog assumed is not in the code.

The 2026-05-15 `credit-card-model-upgrade.md` spec already proposed the field names that the new backlog needs (`statement_closing_day`, `payment_due_day`) but explicitly **deferred per-CC cycle bucketing to V2** ("if users complain"). The new backlog is that complaint — the user wants per-CC cycles now so overrides can actually shift a CC's statement period without dragging every other account along.

This spec proposes:
1. **Slice 1 — Substrate.** Make `close_day` and a new `payment_day` actually drive per-CC cycles, layered on top of (not replacing) `BillingPeriod`. Computed, not stored: no transaction-to-cycle FK.
2. **Slice 2 — Configurable payment day** (the backlog's feature #2).
3. **Slice 3 — Per-month overrides table** (backlog #3, the substrate every other feature rides on).
4. **Slice 4 — Current-month edit shortcut on the account editor** (backlog #4, pure UX on top of Slice 3).

The broader 2026-05-15 CC model upgrade scope (`credit_limit`, `apr`, `payment_strategy`, `payment_source_account_id`) is out of scope for this spec — it ships separately and on its own merits.

## Discovery findings (verified 2026-05-28)

| Concern | Owner backlog assumed | Code reality | Evidence |
|---|---|---|---|
| Per-CC close day | Drives that CC's billing cycle | Stored but unused | `backend/app/models/account.py:62`; `backend/app/services/billing_service.py:47,124` ignores it |
| Cycle assignment for transactions | Each tx lands in a CC cycle until close, then rolls over | No tx ↔ cycle link at all; date-range query at read time | `backend/app/models/transaction.py` (no FK), `backend/app/services/forecast_service.py` |
| "1st of next month" payment date | Implicit but real | No payment-date computation tied to account close/payment fields | `replace(day=1)`/`relativedelta(months=1)` hits exist in `forecast_service.py`, `scenario_engine.py`, `date_utils.py`, `budget_rebalance_service.py`, etc., but none of them compute a payment date from a CC account's close/payment fields |
| Inclusive vs exclusive close day | Decision pending | Moot — no per-CC cycle logic exists | n/a |
| Cycle granularity | Per-account | Org-wide; one `BillingPeriod` row per org per period, shared across all accounts | `backend/app/models/billing.py`, `backend/app/models/user.py:28` |
| Frontend | Just exposes close day | Same; the form is honest about the field | `frontend/app/accounts/page.tsx:37,171,225,362-407` |

Prior 2026-05-13 claude-mem observation (ID 13533) "Account.close_day Is Informational Only" confirms this is a long-standing known gap that simply was not prioritized.

## Relationship to the 2026-05-15 spec

`specs/credit-card-model-upgrade.md` proposed seven new fields on `accounts`:

| Field | This spec | 2026-05-15 spec |
|---|---|---|
| `credit_limit` | out of scope | in scope |
| `statement_closing_day` (rename of `close_day`) | **renaming optional**; the existing `close_day` column is fine if we just wire it up | in scope |
| `payment_due_day` | **in scope** (backlog feature #2) | in scope |
| `payment_strategy` | out of scope | in scope |
| `fixed_payment_amount` | out of scope | in scope |
| `payment_source_account_id` | out of scope (separate foundation slice) | in scope, but flagged as foundation prereq |
| `apr` | out of scope | in scope |

**Recommendation:** treat the 2026-05-15 spec as the source for `credit_limit` / `apr` / `payment_strategy` / `payment_source_account_id` / `fixed_payment_amount` (ships independently when prioritized), and treat this spec as the source for the cycle substrate + overrides + payment day. The two specs overlap on `payment_due_day` only; whichever lands first should land that column.

## Architect decisions (proposed; flag any to revisit)

### D1 — Per-CC cycle vs org-level cycle for credit-card transactions

**Proposed:** **per-CC cycle for credit-card accounts only.** Non-CC accounts (checking, savings, loan, etc.) continue to use `Organization.billing_cycle_day` and the org-wide `BillingPeriod`. Credit-card accounts get a *computed* per-account cycle derived from their own `close_day` + `payment_day` (+ overrides).

**Why:** the owner backlog cannot work otherwise. Overriding "this month's close day to the 24th because the 25th was a Saturday" only makes sense if that close day actually drives a cycle that belongs to that one card. Forcing the org-level cycle to bend for one credit card would break every other account's reporting.

**How to apply:** compute a `CreditCardCycle(account_id, period_start, period_end, payment_date)` triple on demand. Do not introduce a new persisted `cc_billing_periods` table; the cycle is fully derivable from `account.close_day` + `account.payment_day` + any `cc_cycle_override` rows (see Slice 3).

**Supersedes** the 2026-05-15 spec's "Lean (a) for V1, surface (b) as a future enhancement if users complain" — the complaint has been filed.

### D2 — Inclusive vs exclusive close day

**Proposed:** **inclusive.** A transaction dated exactly on the close day belongs to the closing cycle, not the next one.

**Why:** matches every major issuer (Amex, Chase, Citi, Visa-network statements) — the close day is the last day captured on the statement. A user who buys something on the 25th when their close day is the 25th expects to see it on this month's statement.

**How to apply:** `period_end_inclusive = close_day` in the cycle derivation. Cycle compare uses `tx.date >= period_start AND tx.date <= period_end_inclusive`, not `< period_end`.

### D3 — Default payment day

**Proposed:** **1st of the calendar month after the close month**, configurable per account. So if close is Jan 25, default payment is Feb 1; if close is Feb 28, default payment is Mar 1.

**Why:** matches the owner backlog's framing of "implicit 1st of next month" — that becomes the default, not a hardcode. Same-month payment is allowed (e.g. close 1st of month, pay 25th of same month), which covers issuers whose cycle and due-date are both in the same calendar month.

**How to apply:** when a credit-card account is created, both `payment_day` and `payment_day_relative_month` are left NULL; the resolver treats NULL as "default" (`payment_day = 1`, `payment_day_relative_month = 1`, i.e. 1st of next month). User can write either or both to override the defaults. The cycle resolver computes the actual payment date in two steps so the clamp is explicit and the spec is internally consistent:

```python
import calendar
from datetime import date

def _resolve_payment_date(close: date, payment_day: int, payment_day_relative_month: int) -> date:
    # Step 1 — target year/month: walk `payment_day_relative_month` months
    # forward from the close month (relative_month=0 means same month as
    # close, 1 means the following calendar month, etc.).
    months_offset = close.month - 1 + payment_day_relative_month
    target_year = close.year + months_offset // 12
    target_month = months_offset % 12 + 1
    # Step 2 — clamp the day-of-month to the target month's length so
    # `payment_day=31` lands on Feb 28/29, Apr 30, etc., instead of
    # raising ValueError. Same clamp pattern PFV already uses for
    # `Organization.billing_cycle_day` (see `lib/date_utils.py`).
    last_day = calendar.monthrange(target_year, target_month)[1]
    return date(target_year, target_month, min(payment_day, last_day))
```

The clamp belongs in the resolver, never in stored data — overrides record the user's stated intent (`payment_day = 31`), and the resolver decides what that means in February.

### D4 — Override cascade: independent or linked?

**Proposed:** **independent.** Overriding the close day does not auto-shift the payment day, and vice versa. User decides which one(s) to shift per month.

**Why:** banks shift these for different reasons (close shifts when issuer's processing day lands on a weekend; due-date shifts when the post office is closed on a holiday). Cascading would create surprising side effects when the user only wanted to shift one. The UX in Slice 4 can offer a "shift both by N days" convenience action that writes two independent override rows.

### D5 — Gap expenses on a shifted close day

**Proposed:** **closed cycle includes everything up to the *effective* (overridden) close date.** If the user shifts close from the 25th to the 24th for January, the January cycle ends on the 24th; the purchase on the 25th lands in February's cycle.

**Why:** mirrors what the bank does. The owner backlog calls this out as an "open question" — but in practice every issuer applies the shift on the closing side. Read-time-only — no historical rewriting; this only affects how the cycle math runs going forward.

### D6 — Override scope: current only, or future-allowed?

**Proposed:** **current and future allowed. Past months are read-only.** User can pre-record an override for a future month if they know the bank will shift (e.g., next month's close lands on a Saturday).

**Why:** removes a UX gotcha — users often see the upcoming shift before it happens. Past-month read-only because reopening closed cycles would mutate already-displayed history and break reconciliation.

### D7 — Storage shape

**Proposed:** **`cc_cycle_overrides` table:** `(id, account_id FK accounts ON DELETE CASCADE, period_anchor_year SMALLINT, period_anchor_month TINYINT, close_day_override TINYINT NULL, payment_day_override TINYINT NULL, payment_day_relative_month_override TINYINT NULL, created_at, updated_at, PRIMARY KEY (id), UNIQUE (account_id, period_anchor_year, period_anchor_month))`.

- `period_anchor_(year, month)` identifies which CC billing cycle this override applies to, anchored on the cycle's close month.
- At least one of the three `*_override` columns must be non-null (CHECK constraint), otherwise the row is meaningless.
- No transaction-level FK; transactions remain unaware of cycles.
- No persistent `cc_billing_periods` table; the cycle is fully derivable.

**Migration:** single alembic file, ~30 lines, adds the table + adds `accounts.payment_day TINYINT NULL` + `accounts.payment_day_relative_month TINYINT NULL`. **No DB-level default on either column** — the resolver owns the "NULL means default" semantic (see D3). A server default would silently fill `1` on insert and break the "NULL = use resolver default" invariant the moment any other code path opted out of explicit assignment. Both columns are NULL on non-CC accounts (mirrors the existing `close_day` invariant) and NULL-by-default on CC accounts until the user explicitly sets them. Defaults are wired in (a) the validation layer on create and (b) `cc_cycle_service` at resolve time.

### D8 — Where the cycle math lives

**Proposed:** new service `backend/app/services/cc_cycle_service.py`. Single responsibility: given an `account_id` and a target date, return the `CreditCardCycle(period_start, period_end_inclusive, payment_date, source: Literal["default", "override"])`. All readers (forecast, dashboard tile, reconciliation, future statement-balance views) go through this service. No reimplementation elsewhere.

## Slice-by-slice scope

### Slice 1 — Per-CC cycle substrate (new prereq, was implicit in the backlog)

- Add `payment_day` + `payment_day_relative_month` columns to `accounts` (migration N).
- Add `cc_cycle_service.py` with the cycle resolver.
- Wire `close_day` (and the new payment columns) into validation per the existing `validate_create_close_day` / `validate_close_day_cascade` pattern.
- No UI surfacing yet beyond "we now compute a cycle"; the form changes ship in Slice 2.
- **Test plan:** unit tests on cycle resolver covering: (a) default close 25, payment 1st next month; (b) close on 31 in a 30-day month (clamp); (c) close on 31 in February (clamp); (d) same-month payment (close 1, pay 25); (e) leap year Feb 29 close.

### Slice 2 — Configurable payment day (backlog feature #2)

- Frontend: account create/edit form shows Payment day input + "Month after close" / "Same month as close" toggle, gated to CC accounts only (same gating as `close_day` today).
- Backend: `POST/PUT /api/v1/accounts` accepts `payment_day` + `payment_day_relative_month` per the validation pattern.
- Default: `payment_day=1, payment_day_relative_month=1` (i.e. 1st of next month).
- **Test plan:** the existing `validate_create_close_day` + `validate_close_day_cascade` test families gain payment-day siblings.

### Slice 3 — Per-month overrides table (backlog feature #3, the substrate)

- Migration N+1: create `cc_cycle_overrides` per D7.
- New endpoint `POST/PUT/DELETE /api/v1/accounts/{id}/cycle-overrides/{year}/{month}` (org-scoped, owner/admin only).
- `cc_cycle_service` consults the table; override row wins over account defaults.
- Frontend: new "Billing cycle overrides" section on the CC account detail page (NOT the editor) — table of upcoming/past overrides with edit/delete.
- **Test plan:** override resolver tests — override present / absent / partial (close only, payment only); past-month write rejected (409); future-month write accepted.
- Audit events `account.cycle_override.created / updated / deleted` per the existing `audit_service` pattern (snapshot actor + target account, written via the L4.7 `audit_events` table).

### Slice 4 — Current-month edit shortcut (backlog feature #4)

- On the account editor (when editing a CC), surface a "This month's cycle" mini-panel with the current cycle's effective close + payment dates and inline shift controls.
- **Implementation: writes a `cc_cycle_overrides` row for the current period anchor; not a separate code path.** Per the owner's explicit note in the backlog memo ("Must be implemented on top of #3's per-month override mechanism").
- Visual affordance: shifted dates render in italic with a "← reset to default" link that deletes the override.
- **Test plan:** integration test that the inline shift creates the same row shape as the dedicated overrides endpoint; reset deletes it.

## Out of scope (call-outs)

- Statement balance display (computed view, not stored): deferred to the 2026-05-15 spec's slice.
- `credit_limit`, `apr`, `payment_strategy`, `payment_source_account_id`: 2026-05-15 spec's slice; ships independently.
- Forecast integration of the per-CC cycle (the dashboard tile already groups by org cycle; rendering CC payments on their actual due date is a follow-up).
- Statement closing transaction generation, interest accrual, minimum payment computation: rejected per the 2026-05-15 spec's "PFV is a planning tool, not a bank" principle. Reaffirmed here.
- Backfill of `payment_day` for existing CC accounts: not needed; NULL means "use defaults", resolver handles it.

## Effort estimate

| Slice | Effort | Notes |
|---|---|---|
| 1 — Substrate | M (0.5-1 day) | Pure backend + resolver + tests. No UI. |
| 2 — Payment day | S (1-4h) | Form field + validation. Small migration if not bundled with Slice 1. |
| 3 — Overrides | M (0.5-1 day) | Migration + endpoint + detail-page table UI + audit events. |
| 4 — Shortcut | S (1-4h) | Pure UI on top of Slice 3's endpoint. |
| **Total** | **L (1.5-2 days)** | One PR per slice; spec PR (this one) ships separately first. |

Single bundled PR feasible if scope is held tight (no creeping into 2026-05-15 spec's territory), but per the architect bundling principle in the 2026-05-15 spec ("keeps each review small, lets either liability UX ship without blocking the other"), the 4-PR slicing is preferred.

## Sequencing

1. **This spec PR** lands first. Architect-locks D1-D8.
2. Slice 1 (substrate) — backend-only PR. Includes the columns + the resolver + tests.
3. Slice 2 (payment day) — frontend + backend PR.
4. Slice 3 (overrides) — migration + endpoint + detail-page UI + audit events.
5. Slice 4 (shortcut) — UI-only PR on top of Slice 3.

## Cross-references

- `specs/credit-card-model-upgrade.md` (2026-05-15) — companion spec for `credit_limit` / `apr` / `payment_strategy` / `payment_source_account_id`. Same architect lock for "PFV is a planning tool, not a bank."
- `specs/billing-cycle-design.md` — original org-level billing cycle decision. This spec layers on top; does not replace.
- `specs/billing-settled-date-design.md` — settled-date semantics, relevant when Slice 1's resolver decides cycle membership.
- Memory: `project_credit_card_billing_backlog.md` — owner backlog this spec answers.
- Memory: claude-mem ID 13533 (2026-05-13) — prior "close_day is informational only" observation.
