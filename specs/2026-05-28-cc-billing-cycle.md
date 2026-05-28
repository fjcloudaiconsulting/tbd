---
name: Credit Card Billing Cycle — Discovery + Substrate + Overrides
description: 2026-05-28 spec following the 2026-05-27 owner backlog. Reconciles the close-day-is-unused discovery finding with the existing 2026-05-15 CC model upgrade spec, locks the architect decisions, and proposes the per-CC cycle substrate that the user-visible overrides feature requires.
type: project
---

# Credit Card Billing Cycle — Discovery + Substrate + Overrides

**Date:** 2026-05-28. **Status:** discussion-grade, pre-implementation.
**Replaces:** nothing (extends `specs/credit-card-model-upgrade.md` from 2026-05-15).
**Linked memory:** `project_credit_card_billing_backlog.md` (the 2026-05-27 owner backlog this responds to).

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
| "1st of next month" payment date | Implicit but real | Does not exist anywhere | grep of `replace(day=1)`/`relativedelta` returns no hits on accounts |
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

**How to apply:** when a credit-card account is created, `payment_day` defaults to `1` and `payment_day_relative_month` defaults to `1` (next month). User can change both. The cycle resolver computes the actual payment date as `date(close.year + (close.month + payment_day_relative_month - 1) // 12, ((close.month + payment_day_relative_month - 1) % 12) + 1, payment_day)`, clamped to last-day-of-month for short months.

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

**Migration:** single alembic file, ~30 lines, adds the table + adds `accounts.payment_day TINYINT NULL` + `accounts.payment_day_relative_month TINYINT NULL DEFAULT 1`. Both columns NULL on non-CC accounts (mirrors the existing `close_day` invariant).

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
