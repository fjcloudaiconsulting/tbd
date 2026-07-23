---
name: Credit Card Statement Alerts — V1 (auto-close + pre-close alert)
description: 2026-07-23 design. Two per-account scheduler jobs that alert users before and at credit-card statement close. Alert-only (no persisted statement), dedicated notification category with per-user opt-out, per-org scheduler gate. Group C item 4 of the financial-primitives chain.
type: project
---

# Credit Card Statement Alerts — V1

**Date:** 2026-07-23. **Status:** design, approved for planning.
**Roadmap:** Group C item 4 (financial-primitives chain), operator-chosen next target.
**Builds on:** CC Model V1 (`cc_cycle_service`, `cc_forecast_service`, migrations 073–075),
the scheduled-tasks subsystem (#514/#516), and the notification-preferences subsystem.

## TL;DR

Credit-card statements close on each card's own cycle, but nothing in the app tells the
user when. This adds two scheduler-driven alerts per credit-card account:

1. **Pre-close reminder** — ~2 days before the statement closes: *"Your [card] statement
   closes in 2 days."*
2. **Close-day notification** — at close: *"Your [card] statement closed; $X due on
   [payment date]."*

Both are dual-channel (in-app + email), gated by a new dedicated **"Credit card
statements"** notification category with a per-user opt-out, and run only for orgs whose
per-org scheduler toggle is on (default on).

"Auto-close" is **alert-only** in V1: no statement record is persisted. The cycle is
already derivable on demand (`cc_cycle_service`) and the statement balance is already
computable as-of-close (`cc_forecast_service.balance_at_close`), so V1 fires notifications
rather than materializing a new table. Persisting a statement snapshot (history,
drift-locking) is a deliberate V2 item.

## Decisions (locked with operator, 2026-07-23)

| # | Decision | Choice |
|---|---|---|
| D1 | What "auto-close" does | **Alert-only.** No persisted statement/period record. |
| D2 | Alert moments | **Pre-close reminder + close-day notification** (two moments). |
| D3 | Timing anchors | Reminder anchors on **close date** (`period_end_inclusive`); close notification fires at close and names the **due date** (`payment_date`). |
| D4 | Channels | In-app + email, via a **new dedicated notification category** `cc_statement` ("Credit card statements"), per-user `email_` + `in_app_` toggles, **default ON**. |
| D5 | Per-org scheduler gate | New `scheduler.automate_cc_statement_alerts`, **default ON** (consistent with the existing billing jobs; first-tick herd already capped by #516). |
| D6 | Reminder lead days | New `scheduler.cc_statement_reminder_lead_days`, **default 2**, per-org configurable. |
| D7 | Delivery | **One PR.** Category naming stays **CC-specific** (loans, deferred, add their own later). |

## Architecture

### Two jobs, per-account fan-out

The existing scheduler jobs operate **per-org** (on `Organization.billing_cycle_day`).
Credit-card cycles are **per-account** (each card has its own `close_day`/`payment_day`).
So two new jobs are added, each operating at **org granularity** on the outside (to fit the
runner's `(org, job)` sweep and the `max_orgs` rollout cap) but **fanning out internally**
over the org's active credit-card accounts:

- `backend/app/services/scheduler/jobs/cc_statement_reminder.py` — `CcStatementReminderJob`
  - `job_type = "cc_statement_reminder"`
  - `setting_key = org_settings.AUTOMATE_CC_STATEMENT_KEY`
- `backend/app/services/scheduler/jobs/cc_statement_close.py` — `CcStatementCloseJob`
  - `job_type = "cc_statement_closed"`
  - `setting_key = org_settings.AUTOMATE_CC_STATEMENT_KEY`

Both share **one** per-org toggle, exactly as `BillingReminderJob`/`BillingCloseJob` share
`AUTOMATE_BILLING_KEY`. Distinct `job_type` strings keep their failure-audit lanes separate
(the reason `billing_reminder` is distinct from `billing_close`). Both are appended to
`REGISTRY` in `runner.py`:

```python
REGISTRY = [
    RecurringGenerationJob(),
    BillingReminderJob(),
    BillingCloseJob(),
    CcStatementReminderJob(),
    CcStatementCloseJob(),
]
```

Each job:

- **`is_due(db, org, today)`** — loads the org's active credit-card accounts (a helper,
  `_active_cc_accounts(db, org_id)`), and returns `True` if **any** card has an unsent
  alert due today. Pure resolver math + a bounded audit-dedup check; no notifications, no
  mutations. Returning `True` here is what makes the org count against the `max_orgs` cap,
  so the check must be cheap and side-effect-free.
- **`run(db, org, today)`** — iterates the org's due cards; for each, resolves the cycle,
  fires the per-card notification, writes the dedup audit row, and commits. **Per-card
  isolation:** each card is processed in its own `try/except`; a single card's failure
  writes a per-card failure audit and continues, so one bad card never suppresses alerts
  for the others (mirrors the runner's per-job resilience one level down).

`_active_cc_accounts` selects accounts where the type slug is `credit_card`
(`close_day IS NOT NULL` is the schema invariant) and the account is active (not
archived/closed) — the exact active-account predicate is confirmed against the Account
model at implementation time and reused, not re-derived.

### Alert timing and idempotency

There is no statement table, so dedup uses the **audit-row-as-marker** pattern already
established by `billing_reminder` (`record_reminder` / `reminder_already_sent`), extended
with `account_id` in the key. New helpers in
`backend/app/services/scheduler/audit.py`:

```python
CC_REMINDER_EVENT_TYPE = "scheduler.cc_statement.reminder"
CC_CLOSED_EVENT_TYPE   = "scheduler.cc_statement.closed"

async def record_cc_alert(*, org, account_id, close_date, event_type, detail) -> int | None
async def cc_alert_already_sent(db, org_id, account_id, close_date, event_type) -> bool
```

The audit `detail` carries `account_id` and `close_date` (ISO). The "already sent" check
queries audit rows by `event_type` + `target_org_id`, then matches
`detail.account_id == account_id and detail.close_date == close_iso`.

**Improvement over the inherited pattern:** `reminder_already_sent` scans *all* audit rows
of its type for the org (unbounded over time). The CC variant adds a
`AuditEvent.created_at >= today - timedelta(days=40)` bound — we only ever dedup against the
current cycle, so a 40-day window is always sufficient and keeps the scan small as history
grows.

**Due conditions:**

- **Reminder** — for a card, resolve `cycle = resolve_cycle_for_account(account, today)`;
  `days_until = (cycle.period_end_inclusive - today).days`; due when
  `0 < days_until <= lead_days` **and** not `cc_alert_already_sent(..., CC_REMINDER_EVENT_TYPE)`
  for `(account_id, cycle.period_end_inclusive)`.
- **Close** — for a card, the just-closed cycle is the one whose `period_end_inclusive` is
  the most recent close on/before `today`. Resolve it (walk back one day from `today` if
  `today` is past this month's close, else it is this month's cycle — a small helper
  `_most_recent_closed_cycle(account, today)` built on `resolve_cycle_for_account`). Due
  when that cycle's `period_end_inclusive <= today` **and** not
  `cc_alert_already_sent(..., CC_CLOSED_EVENT_TYPE)` for `(account_id, close_date)`.

Idempotency is derived entirely from durable audit state: catch-up after downtime fires each
alert once, and a double-tick never double-sends.

### Close-alert amount (reuse Slice 3)

The close notification's "$X due" reuses the CC-forecast building blocks so the alerted
figure is **consistent with the forecast** and correct for grace-period cycles:

```python
ledger      = <transactions with balance_contribution_filter(), cash-basis eff_date>
b_k         = cc_forecast_service.balance_at_close(opening_balance, ledger, close_date)
statement   = cc_forecast_service.outstanding_at_close(b_k)   # >= 0, owed stored negative
due_date    = cycle.payment_date
```

The ledger is built with `balance_contribution_filter()` (the Slice-3 discriminator that
keeps real transfer legs and drops reconcile-matched duplicates) — the same filter the
forecast service uses, so `statement` matches what the forecast would bill. The amount is
formatted in the **card's own currency** (no FX in V1). If the ledger-loading step is not
already exposed as a reusable function, a small `load_cc_ledger(db, account, up_to)` helper
is extracted alongside the existing forecast query rather than duplicated.

### Notification category and per-user preferences

- New enum member `NotificationCategory.CC_STATEMENT = "cc_statement"` in
  `backend/app/models/notification.py`.
- **Migration 076** — add two boolean columns to the notification-preferences table:
  `email_cc_statement` and `in_app_cc_statement`, **`server_default` true** (users opt out;
  they don't opt in). Mirrors the existing `email_org_activity`/`in_app_org_activity`
  column shape. MySQL + SQLite-CI compatible (plain boolean columns, no ENUM).
- Wire the category→preference maps in `notification_service.py`
  (`_IN_APP_PREF_BY_CATEGORY`, `_EMAIL_PREF_BY_CATEGORY`): `CC_STATEMENT -> in_app_cc_statement`
  / `email_cc_statement`.
- Extend the preferences Pydantic schema + `GET/PUT` preferences endpoint with the two new
  fields (default true; no always-on carve-out — that is `security` only).
- Add a category row to `frontend/app/settings/notifications/page.tsx`:
  `id: "cc_statement"`, title **"Credit card statements"**, copy e.g. *"Reminders before
  your credit-card statement closes, and a summary of what's due when it does,"*
  `emailKey: "email_cc_statement"`, `inAppKey: "in_app_cc_statement"`.

Dispatch uses the existing `dispatch_notification_to_org_members(..., category=CC_STATEMENT)`.
As with the other scheduler notifications, alerts go to **all org members** (for a solo user,
that is just them); each member's own `cc_statement` preferences gate their in-app + email
delivery.

### Per-org scheduler settings

In `backend/app/services/scheduler/org_settings.py`:

```python
AUTOMATE_CC_STATEMENT_KEY          = "scheduler.automate_cc_statement_alerts"
CC_STATEMENT_REMINDER_LEAD_DAYS_KEY = "scheduler.cc_statement_reminder_lead_days"

_BOOL_DEFAULTS = {
    AUTOMATE_RECURRING_KEY: "true",
    AUTOMATE_BILLING_KEY: "true",
    AUTOMATE_CC_STATEMENT_KEY: "true",   # default ON (D5)
}
_CC_STATEMENT_LEAD_DEFAULT = 2           # default 2 days (D6)

async def get_cc_statement_lead_days(db, org_id) -> int: ...
```

Extend:

- `org_settings.get_all()` — add `automate_cc_statement_alerts` +
  `cc_statement_reminder_lead_days`.
- `GET/PUT /api/v1/scheduler/settings` (the `require_org_admin` endpoint) — accept and
  return the two new fields, with the same typed-accessor writes (not the generic settings
  writer).
- `frontend/components/.../SchedulerSettingsCard.tsx` ("Automatic tasks") — add the toggle
  ("Credit-card statement alerts") + the lead-days number input.

### Notification templates and link

Two new templates in `backend/app/services/notification_templates.py`, returning
`(title, body, link)`:

- `scheduler_cc_statement_reminder(card_name, close_date, days_until)` →
  *"[card] statement closes in N days"* / body naming the close date.
- `scheduler_cc_statement_closed(card_name, statement_balance, payment_date)` →
  *"[card] statement closed"* / body naming the amount + due date.

`link` → `/accounts?edit=<account_id>` (the #570 client-only deep-link that opens that
card's inline editor). Copy follows the house style: no em-dashes, no AI attribution.

## Data flow

```
scheduler tick (every SCHEDULER_TICK_SECONDS)
  └─ run_all_due(today, max_orgs=25)
       └─ per org, per job in REGISTRY:
            CcStatementReminderJob / CcStatementCloseJob
              ├─ skip if scheduler.automate_cc_statement_alerts is off
              ├─ is_due(org): any active CC account with an unsent due alert?
              └─ run(org): for each due CC account:
                   ├─ resolve cycle (cc_cycle_service)
                   ├─ [close job] statement = outstanding_at_close(balance_at_close(...))
                   ├─ dispatch_notification_to_org_members(category=CC_STATEMENT)
                   ├─ record_cc_alert(...)  ← dedup marker (audit row)
                   └─ commit   (per-card try/except → failure audit + continue)
```

## Testing

- **Resolver / due-logic unit tests** — reminder window boundaries (`days_until` = 0,
  1, `lead`, `lead+1`); close-day trigger on and after `period_end_inclusive`; dedup
  suppression (second tick same cycle is a no-op); catch-up idempotency (a tick days after
  close still fires exactly once); short-month/leap-year clamps flow through the existing
  resolver.
- **Statement-amount test** — `balance_at_close` / `outstanding_at_close` produce the
  expected owed figure for a seeded ledger; grace-period cycle (post-close purchases) does
  not inflate the alerted balance.
- **Job-level tests** — monkeypatch collaborators by module path (the existing
  SQLite-in-memory job-test idiom); assert notifications dispatched per due card, audit
  markers written, off-toggle short-circuits, per-card failure isolation.
- **Migration 076** — verified up / down / up on **real MySQL** (columns added with
  `server_default` true, dropped on downgrade), not just SQLite CI.
- **Preferences + settings tests** — new prefs fields default true and round-trip through
  `GET/PUT`; scheduler-settings endpoint round-trips the toggle + lead days.
- **Frontend** — notifications-page row renders + toggles; `SchedulerSettingsCard` renders
  + persists the new controls; `tsc --noEmit`, `eslint --quiet`, design-token check all
  clean.

## Out of scope (V1)

- **Persisted statement snapshot / history** — deferred V2 (the derivable-on-demand model
  is sufficient for alerting).
- **Interest accrual, minimum-payment computation, statement-close transaction generation**
  — rejected per the standing "PFV is a planning tool, not a bank" principle.
- **Pre-*due* payment reminder** ("$X due in 3 days") — a separable payment-reminder feature
  the forecast already partly covers; not bundled here.
- **Loan statement alerts** — Loan V1 is architect-deferred; a broader category can be
  introduced when loans ship.
- **Multi-currency FX** — the alert reports the card's own-currency statement balance; no
  cross-currency conversion.

## Files touched (summary)

**Backend:** `models/notification.py` (enum + 2 pref columns), `alembic/versions/076_*.py`
(migration), `services/notification_service.py` (category maps),
`services/notification_templates.py` (2 templates), `services/scheduler/org_settings.py`
(keys + accessors + `get_all`), `services/scheduler/audit.py` (CC dedup helpers),
`services/scheduler/jobs/cc_statement_reminder.py` + `cc_statement_close.py` (new jobs),
`services/scheduler/runner.py` (`REGISTRY`), the scheduler-settings router + the
notification-preferences schema/router.

**Frontend:** `app/settings/notifications/page.tsx` (category row),
`SchedulerSettingsCard.tsx` (toggle + lead-days), any shared prefs/types.

## Cross-references

- `reference_scheduled_tasks_subsystem` — ticker/runner/toggle/audit model; the
  "CC-close deferred v2" note this spec discharges; the #516 first-tick cap.
- `reference_cc_model_v1` — `cc_cycle_service`, `cc_forecast_service`
  (`balance_at_close`/`outstanding_at_close`/`resolve_cycle_for_account`),
  `balance_contribution_filter` gotcha.
- `specs/2026-05-28-cc-billing-cycle.md` — the cycle substrate this rides on.
- `specs/2026-07-22-cc-model-v1-design.md` — the forecast integration reused for the amount.
