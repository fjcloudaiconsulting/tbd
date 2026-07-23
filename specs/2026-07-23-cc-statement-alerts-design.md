---
name: Credit Card Statement Alerts — V1 (auto-close + pre-close alert)
description: 2026-07-23 design, architect-reviewed (backend/frontend/design/security, all APPROVE-WITH-CHANGES, folded). Two per-account scheduler jobs that alert users before and at credit-card statement close. Alert-only (no persisted statement), dedicated notification category with per-user opt-out, per-org scheduler gate. Group C item 4 of the financial-primitives chain.
type: project
---

# Credit Card Statement Alerts — V1

**Date:** 2026-07-23. **Status:** design, architect-reviewed and revised — approved for planning.
**Roadmap:** Group C item 4 (financial-primitives chain), operator-chosen next target.
**Builds on:** CC Model V1 (`cc_cycle_service`, `cc_forecast_service`, migrations 073–075),
the scheduled-tasks subsystem (#514/#516), and the notification-preferences subsystem.
**Review:** four architect reviews folded — see **§ Architect review resolutions**.

## TL;DR

Credit-card statements close on each card's own cycle, but nothing in the app tells the
user when. This adds two scheduler-driven alerts per credit-card account:

1. **Pre-close reminder** — ~2 days before the statement closes: *"[card] statement closes
   soon."* **In-app only** (a no-money heads-up; kept off email to avoid noise).
2. **Close-day notification** — at close: **in-app** *"[card] statement closed.
   1,240.00 EUR is due on 2026-08-01."* + **email** that omits the amount (*"[card]
   statement closed. Open the app to see what's due."*) with a deep-link into the card.

Both are gated by a new dedicated **"Credit card statements"** notification category with a
per-user opt-out (default ON), and run only for orgs whose per-org scheduler toggle is on
(default on).

"Auto-close" is **alert-only** in V1: no statement record is persisted. The cycle is already
derivable on demand (`cc_cycle_service`) and the statement balance is already computable
as-of-close (`cc_forecast_service.balance_at_close`), so V1 fires notifications rather than
materializing a new table. Persisting a statement snapshot (history, drift-locking) is a
deliberate V2 item.

## Decisions (locked with operator 2026-07-23; D8–D10 added after architect review)

| # | Decision | Choice |
|---|---|---|
| D1 | What "auto-close" does | **Alert-only.** No persisted statement/period record. |
| D2 | Alert moments | **Pre-close reminder + close-day notification** (two moments). |
| D3 | Timing anchors | Reminder anchors on **close date** (`period_end_inclusive`); close notification fires at close and names the **due date** (`payment_date`). |
| D4 | Category + prefs | **New dedicated notification category** `cc_statement` ("Credit card statements"), per-user `email_` + `in_app_` toggles, **default ON** (opt-out). |
| D5 | Per-org scheduler gate | New `scheduler.automate_cc_statement_alerts`, **default ON**. First-tick herd already capped by #516. |
| D6 | Reminder lead days | New `scheduler.cc_statement_reminder_lead_days`, **default 2**, per-org configurable, clamped **[0, 31]** (0 disables the reminder), mirroring the billing lead-days control. |
| D7 | Delivery | **One PR.** Category naming stays **CC-specific**. |
| **D8** | **Channels per moment** (security F2 + design I2) | **Reminder = in-app only** (no email, no amount). **Close = in-app WITH amount + email WITHOUT amount** (email says "open the app to see what's due" + deep-link). The dollar figure never leaves the trust boundary; only one email stream per card per month. |
| **D9** | **Backfill guard** (design C1) | The **close** alert is suppressed for any cycle whose `period_end_inclusive` is **on or before the account's creation date**, so a newly-added / newly-configured card never fires a spurious "statement closed" for a cycle the app never tracked. |
| **D10** | **$0-due close alert** (design M1) | When `outstanding_at_close == 0`: **no email**; in-app note *"[card] statement closed with nothing due."* Dedup marker still written so `is_due` doesn't re-evaluate every tick. |

## Architecture

### Two jobs, per-account fan-out

The existing scheduler jobs operate **per-org** (on `Organization.billing_cycle_day`).
Credit-card cycles are **per-account** (each card has its own `close_day`/`payment_day`).
So two new jobs are added, each operating at **org granularity** on the outside (to fit the
runner's `(org, job)` sweep and the `max_orgs` rollout cap) but **fanning out internally**
over the org's active credit-card accounts:

- `backend/app/services/scheduler/jobs/cc_statement_reminder.py` — `CcStatementReminderJob`
  - `job_type = "cc_statement_reminder"`, `setting_key = org_settings.AUTOMATE_CC_STATEMENT_KEY`
- `backend/app/services/scheduler/jobs/cc_statement_close.py` — `CcStatementCloseJob`
  - `job_type = "cc_statement_closed"`, `setting_key = org_settings.AUTOMATE_CC_STATEMENT_KEY`

Both share **one** per-org toggle, exactly as `BillingReminderJob`/`BillingCloseJob` share
`AUTOMATE_BILLING_KEY`. Distinct `job_type` strings keep their failure-audit lanes separate.
Both are appended to `REGISTRY` in `runner.py`:

```python
REGISTRY = [
    RecurringGenerationJob(), BillingReminderJob(), BillingCloseJob(),
    CcStatementReminderJob(), CcStatementCloseJob(),
]
```

**Fan-out cost note (security F7 / backend M9):** `run_all_due(max_orgs=25)` bounds the
number of *orgs* that do work per tick, and `org_did_work` counts an org once even if both
CC jobs fire (`runner.py:65-67`) — no double-count. Each job's `run()` returns
`JobResult.noop()` when zero cards actually dispatched (all deduped or lost a race between
`is_due` and `run`), so an empty tick does not consume the rollout budget. Per-org card
count is assumed small (a household's handful of cards); the org-batched loader below keeps
per-tick DB cost to O(1) queries per org, not O(cards).

Each job:

- **`is_due(db, org, today)`** — loads the org's active credit-card accounts
  (`_active_cc_accounts`) and, with **one** bounded audit query per run (see Dedup), returns
  `True` if any card has an unsent alert due today. **No balance math and no notifications
  here** — only cheap resolver arithmetic + the in-memory dedup match, so the `max_orgs`
  gate stays cheap and side-effect-free.
- **`run(db, org, today)`** — iterates the org's due cards; for each: resolve the cycle,
  **write the dedup audit marker, then dispatch** the notification, then `db.commit()`
  (marker-first ordering per backend I3, matching `billing_reminder`; the audit write uses
  its own session and never raises, so marker-first closes the double-send window). The
  expensive statement-balance computation happens only here (close job), never in `is_due`.
- **Per-card isolation (backend I4):** each card is processed in its own `try/except`; on
  failure the handler calls **`await db.rollback()`** on the shared session (else the next
  card's commit hits `PendingRollbackError`), writes a per-card failure audit (own session,
  safe), and continues. One bad card never suppresses the others.

`_active_cc_accounts(db, org_id)` selects `Account.is_active.is_(True)` **JOIN**
`AccountType.slug == "credit_card"` with `Account.close_day.isnot(None)`, scoped to
`org_id` (backend M8). It deliberately does **not** require `payment_source_account_id`
(alerting needs no payment target, unlike the forecast). This is a **required `org_id`-filter
review checkpoint** (security F5).

### Close-cycle resolution + backfill guard

The close job must alert on the **most-recently-closed** cycle, resolved correctly even when
`today` is well past the close day (backend I5) and guarded against backfill (design C1):

```python
def _most_recent_closed_cycle(account, today) -> CreditCardCycle | None:
    cyc = resolve_cycle_for_account(account, today)
    if cyc.period_end_inclusive > today:                 # today is in the post-close gap
        cyc = resolve_cycle_for_account(account, cyc.period_start - timedelta(days=1))
    # cyc.period_end_inclusive is now the most recent close on/before today.
    if cyc.period_end_inclusive <= account_creation_date(account):   # D9 backfill guard
        return None
    return cyc
```

Anchor on `cyc.period_start - 1 day`, **never `today - 1 day`** (subtracting one day from a
`today` that is 20 days past close still resolves to the upcoming cycle). The on-close case
already works because the inclusive close means `resolve(close_date).period_end_inclusive
== close_date`. `account_creation_date(account)` is `account.created_at.date()` (confirm the
exact column at implementation; use `opening_balance` accounting epoch if the model exposes a
distinct one). The reminder job needs **no** backfill guard — it only ever fires for an
*upcoming* close, which is always after the card's creation.

### Idempotency / dedup

No statement table, so dedup uses the **audit-row-as-marker** pattern
(`billing_reminder`'s `record_reminder`/`reminder_already_sent`), keyed on
**`account_id` + close-date ISO** carried in the audit `detail`. New helpers in
`backend/app/services/scheduler/audit.py`:

```python
CC_REMINDER_EVENT_TYPE = "scheduler.cc_statement.reminder"
CC_CLOSED_EVENT_TYPE   = "scheduler.cc_statement.closed"

async def record_cc_alert(*, org, account_id, close_date, event_type, detail) -> int | None
async def cc_alerts_sent_since(db, org_id, event_type, since) -> set[tuple[int, str]]
```

**Batched, not per-card (security F7 / backend M12):** `is_due`/`run` fetch the set of
already-sent `(account_id, close_date_iso)` pairs with **one** query per job-run —
`AuditEvent.event_type == <type>`, `target_org_id == org_id`,
`created_at >= today - timedelta(days=40)` — then match in memory across the org's cards.
The 40-day window safely exceeds one monthly cycle + lead (the most-recent-closed cycle is
always within ~31 days), so there is no missed-fire even after long downtime, and the scan
stays bounded as history grows. Detail carries `account_id` + `close_date` only — **no dollar
amount** (security F3). `AuditEvent.created_at` is a server `DateTime`; comparing to a `date`
coerces to midnight (fine on MySQL + SQLite).

Idempotency is derived entirely from durable audit state: catch-up after downtime fires each
alert once; a double-tick never double-sends.

### Statement-balance amount (reuse Slice 3, org-batched)

The close notification's amount reuses the CC-forecast building blocks so it is **consistent
with the forecast** and correct for grace-period cycles:

```python
b_k       = cc_forecast_service.balance_at_close(opening_balance, ledger, close_date)
statement = cc_forecast_service.outstanding_at_close(b_k)   # >= 0, owed stored negative
```

`balance_at_close`/`outstanding_at_close` are DB-free and callable directly, with
`opening_balance = Decimal(str(account.opening_balance))` (backend I6).

**No reusable single-account ledger loader exists today** (the query is inlined in
`account_balance_forecast_service.py:136-148`). Extract an **org-batched** loader
`load_cc_ledgers(db, org_id, account_ids, up_to) -> dict[account_id, list[(eff_date, signed)]]`
that reproduces that query **exactly** — `case`-signed amount, `effective_period_date_expr()`
cash-basis date, `balance_contribution_filter()`, **no status clause** — so the alerted
`statement` cannot drift from what the forecast bills. Per-account `load_cc_ledger` is
rejected as N+1. This loader is a **required `org_id`-filter review checkpoint** (security
F5). The amount is formatted in the **card's own currency** (no FX in V1).

### Notification category and per-user preferences

- New enum member `NotificationCategory.CC_STATEMENT = "cc_statement"` in
  `backend/app/models/notification.py`.
- **Migration 076 — two parts, both verified up/down/up on real MySQL:**
  1. **Native ENUM alter (backend C1, CRITICAL — prod-breaker if omitted).**
     `notifications.category` is a native MySQL `ENUM(...)` (created in `050`, 4 values;
     the model does not set `native_enum=False`). Adding a category and dispatching it fails
     on MySQL while passing SQLite CI. The migration must, **guarded to MySQL**
     (`if op.get_bind().dialect.name == "mysql"`):
     `ALTER TABLE notifications MODIFY COLUMN category
      ENUM('security','account','org_admin','org_activity','cc_statement') NOT NULL`.
     Downgrade must delete/remap any `cc_statement` rows **before** reverting the ENUM to 4
     values (else the reverse MODIFY fails).
  2. **Two boolean preference columns** on `user_notification_preferences`:
     `email_cc_statement` + `in_app_cc_statement`, **`server_default=sa.text("1")`,
     `default=True`** — mirroring `email_account`/`in_app_account` (the default-ON columns),
     **not** `org_activity` (default-OFF) (backend I2). No org-wipe/reset wiring needed —
     the table cascades via `users.id ON DELETE CASCADE` and 076 only adds columns (backend
     M11).
- Wire **both** category→preference maps in `notification_service.py`:
  `_IN_APP_PREF_FIELD[CC_STATEMENT] = "in_app_cc_statement"` and
  `_EMAIL_PREF_FIELD[CC_STATEMENT] = "email_cc_statement"` (backend M7, real map names).
  **Hard checklist item (security F6):** omitting either map falls through to *default-allow /
  force-send*, silently ignoring the user's opt-out.
- Add both fields (default True) to `NotificationPreferencesResponse` +
  `NotificationPreferencesUpdate` (`schemas/notification.py`); no always-on carve-out (that
  is `security` only). Update `_default_preferences`.

### Channels per moment (D8) — dispatch extension

`dispatch_notification_to_org_members` today sends **one** title/body to both channels and
always attempts email when the pref allows. D8 needs (i) an in-app-only send and (ii) an
email body that differs from the in-app body. Extend the helper with two **optional,
backward-compatible** kwargs (defaults preserve every existing caller — billing, recurring,
etc.):

```python
async def dispatch_notification_to_org_members(
    ..., send_email: bool = True, email_body: str | None = None,
): ...
# email uses email_body if provided, else body; email skipped entirely when send_email is False.
```

- **Reminder** → `send_email=False` (in-app only).
- **Close** → in-app `body` includes the amount; `email_body` omits it.
- **$0 close** → `send_email=False`, in-app "nothing due" body.

Preference semantics that result: `in_app_cc_statement` gates both in-app notices;
`email_cc_statement` gates the (single) close email. Both default ON.

### Notification copy (design M2/M3; house voice — no em-dashes, card name in title,
### currency formatted `amount + " " + currency`, ISO dates per billing precedent)

Templates in `notification_templates.py`, returning `(title, body, link)` (close returns an
extra `email_body`):

- `scheduler_cc_statement_reminder(card_name, close_date, days_until)`
  - title: `f"{card_name} statement closes soon"`
  - body: `f"Your {card_name} statement closes on {close_date.isoformat()} (in {days_until} day(s)). We'll send the amount due once it closes."`
- `scheduler_cc_statement_closed(card_name, amount_str, currency, payment_date)`
  - title: `f"{card_name} statement closed"`
  - in-app body (amount > 0): `f"Your {card_name} statement closed. {amount_str} {currency} is due on {payment_date.isoformat()}."`
  - in-app body (amount == 0): `f"Your {card_name} statement closed with nothing due."`
  - email body: `f"Your {card_name} statement closed. Open the app to see what's due."`
  - link: `/accounts?edit=<account_id>`

`amount_str` is the card-currency-formatted magnitude produced backend-side (confirm/introduce
a backend money formatter; the template receives a pre-formatted string) (design I3). The
in-app amount and the accounts-page "Upcoming payments" figure both derive from
`outstanding_at_close`, so they agree (design M4 reconciliation check). "Your [card]" second
person is kept for parity with billing copy; may neutralize to "The [card]" if it grates in
multi-member testing (design M5).

### Per-org scheduler settings

In `backend/app/services/scheduler/org_settings.py`:

```python
AUTOMATE_CC_STATEMENT_KEY           = "scheduler.automate_cc_statement_alerts"
CC_STATEMENT_REMINDER_LEAD_DAYS_KEY = "scheduler.cc_statement_reminder_lead_days"
_BOOL_DEFAULTS = { ...: "true", AUTOMATE_CC_STATEMENT_KEY: "true" }   # default ON (D5)
_CC_STATEMENT_LEAD_DEFAULT, _CC_MIN, _CC_MAX = 2, 0, 31              # clamp like billing (D6/M10)

async def get_cc_statement_lead_days(db, org_id) -> int: ...   # clamp to [_CC_MIN, _CC_MAX]
```

Extend `org_settings.get_all()`, the `GET/PUT /api/v1/scheduler/settings` endpoint
(`require_org_admin`, typed `set_value` accessors — security F6), and bound the field in
`SchedulerSettingsUpdate`.

### Frontend

- **`frontend/lib/types.ts` (frontend F1 — name them):** add
  `email_cc_statement: boolean` + `in_app_cc_statement: boolean` to `NotificationPreferences`;
  add `automate_cc_statement_alerts: boolean` + `cc_statement_reminder_lead_days: number` to
  `SchedulerSettings`.
- **`app/settings/notifications/page.tsx`:** add one `CATEGORIES` row —
  `id:"cc_statement"`, title **"Credit card statements"**, description
  *"Reminders before each credit-card statement closes, and the amount due when it does. On
  by default; turn it off if you would rather not follow along."*,
  `emailKey:"email_cc_statement"`, `inAppKey:"in_app_cc_statement"` (no `locked`). Update the
  now-stale header comments ("four categories" → five, "eight-field" → ten) (frontend M4).
- **`components/settings/SchedulerSettingsCard.tsx` (frontend F2 + design I4):** the card
  holds a **single** `leadDaysDraft` + `commitLeadDays` hardwired to billing. Add a **second
  independent** `ccLeadDaysDraft` + commit handler (seeded in the mount effect), and widen
  the `BooleanField` union (`+"automate_cc_statement_alerts"`) and `savingField` union
  (`+"cc_statement_reminder_lead_days"`). Restructure into **two labeled sub-sections** with
  scoped labels (design I4):
  - "Budget period" — existing toggle; relabel its input **"Days before a budget period
    closes to notify members."**
  - "Credit-card statements" — toggle **"Credit-card statement alerts"**, helper *"Send a
    reminder before each card's statement closes, and a summary of what's due when it does."*;
    lead-days input **"Days before a card statement closes to remind members,"** min **0**
    max **31**, hint *"0 to 31 days. Only sent while credit-card statement alerts are enabled
    above."* (0-disables documented, billing parity; frontend F3).
- No SWR hooks involved — both surfaces use `useState`/`useEffect`/`apiFetch` +
  `get/updateSchedulerSettings` (frontend M5).
- Deep-link `returnTo`: verify an email link clicked while logged-out preserves the
  `?edit=<id>` query through the returnTo sanitizer (frontend M7).

## Data flow

```
scheduler tick → run_all_due(today, max_orgs=25)
  └─ per org, per job (CcStatementReminderJob / CcStatementCloseJob):
       ├─ skip if scheduler.automate_cc_statement_alerts is off
       ├─ is_due(org): active CC accounts + ONE bounded audit query → any unsent due card?
       │               (resolver math only; NO balance math, NO dispatch)
       └─ run(org): for each due card (own try/except; rollback+failure-audit+continue on error):
            ├─ resolve cycle (+ backfill guard on close, D9)
            ├─ [close] statement = outstanding_at_close(balance_at_close(org-batched ledger))
            ├─ record_cc_alert(...)          ← marker FIRST (backend I3)
            ├─ dispatch_notification_to_org_members(category=CC_STATEMENT,
            │        send_email=<reminder:False / close:True/False-if-$0>, email_body=<close only>)
            └─ db.commit()
          → JobResult.noop() if zero cards dispatched (backend M9)
```

## Testing

- **Due-logic unit tests** — reminder window boundaries (`days_until` = 0, 1, `lead`,
  `lead+1`); close trigger on and after `period_end_inclusive`; **backfill guard: a card
  created 3 days after its close_day does NOT fire a close alert for the pre-creation cycle**
  (design C1); `_most_recent_closed_cycle` correct when `today` is 1 day and 20 days past
  close (backend I5); short-month/leap-year clamps via the existing resolver.
- **Dedup** — second tick same cycle is a no-op; catch-up days later fires exactly once; the
  batched `cc_alerts_sent_since` set-match suppresses correctly.
- **Amount** — `balance_at_close`/`outstanding_at_close` produce the expected owed figure;
  grace-period post-close purchases do not inflate it; `$0` → in-app "nothing due", no email
  (D10).
- **Channels** — reminder sends in-app only (no email); close sends in-app-with-amount +
  email-without-amount; per-user `email_cc_statement=False` suppresses the close email;
  `in_app_cc_statement=False` suppresses both in-app notices; the `dispatch_...` extension
  defaults leave existing callers unchanged.
- **Job-level** — monkeypatch collaborators (SQLite-in-memory idiom); off-toggle
  short-circuits; per-card failure isolation rolls back and continues; `JobResult.noop()` on
  empty tick.
- **Migration 076** — ENUM alter + both pref columns verified **up/down/up on real MySQL**
  (not just SQLite); downgrade remaps `cc_statement` rows before reverting the ENUM.
- **Preferences + settings** — new prefs default true and round-trip GET/PUT; both maps wired
  (opt-out actually suppresses); scheduler-settings round-trips the toggle + clamped lead-days.
- **Frontend** — notifications row renders/toggles; `SchedulerSettingsCard` two sub-sections
  render + persist independently (no cross-wiring of the two lead-days); widen existing mock
  fixtures in `tests/scheduler-settings.test.tsx` + `tests/app/settings-notifications-page.test.tsx`
  to the new shape; `tsc --noEmit`, `eslint --quiet`, design-token check clean.

## Out of scope (V1)

- Persisted statement snapshot / history (deferred V2).
- Interest accrual, minimum-payment computation, statement-close transaction generation
  (rejected — "PFV is a planning tool, not a bank").
- Pre-*due* payment reminder ("$X due in 3 days") — separable payment-reminder feature.
- Loan statement alerts (Loan V1 architect-deferred).
- Multi-currency FX — card's own-currency balance only.
- Per-moment email preference granularity — D8 fixes moment→channel policy in code (reminder
  in-app-only), not via separate per-moment user toggles.

## Files touched (summary)

**Backend:** `models/notification.py` (enum + 2 pref columns, default ON),
`alembic/versions/076_*.py` (ENUM alter + 2 columns), `services/notification_service.py`
(both pref maps + `dispatch_...` `send_email`/`email_body` extension),
`services/notification_templates.py` (2 templates + money formatting),
`services/scheduler/org_settings.py` (keys + clamped accessor + `get_all`),
`services/scheduler/audit.py` (CC dedup helpers, batched),
`services/scheduler/jobs/cc_statement_reminder.py` + `cc_statement_close.py`,
`services/scheduler/runner.py` (`REGISTRY`), org-batched ledger loader (extracted from
`account_balance_forecast_service`), the scheduler-settings router (+ `SchedulerSettingsUpdate`
bound) and the notification-preferences schema.

**Frontend:** `lib/types.ts` (2 interfaces), `app/settings/notifications/page.tsx` (row +
comment counts), `components/settings/SchedulerSettingsCard.tsx` (second draft-state + two
sub-sections + union widenings), existing test fixtures widened.

## Architect review resolutions (2026-07-23)

Four independent architect reviews of the pre-revision spec, all **APPROVE-WITH-CHANGES**;
every actionable finding folded above. Recorded so the guidance that shaped the build is not
lost.

**Backend — verdict APPROVE-WITH-CHANGES.**
- *C1 (CRITICAL, folded):* migration must ALTER the native MySQL `notifications.category`
  ENUM to add `cc_statement`, MySQL-guarded, with a safe downgrade — else SQLite CI is green
  while prod 500s on first dispatch. → § Migration 076.
- *I2 (folded):* default-ON columns must mirror `email_account` (`server_default text("1")`),
  not `org_activity` (OFF). *I3 (folded):* marker-write **before** dispatch, commit last
  (double-send window). *I4 (folded):* per-card `except` must `db.rollback()` the shared
  session. *I5 (folded):* `_most_recent_closed_cycle` anchors on `period_start - 1 day`.
  *I6 (folded):* extract an **org-batched** ledger loader reproducing the forecast query
  exactly; no per-account N+1. *M7/M8/M9/M10/M11/M12 (folded):* real map names, `is_active`
  predicate (no `payment_source` requirement), `JobResult.noop()` on empty tick, `[0,31]`
  lead clamp, `server_default text("1")` + no org-wipe wiring, 40-day dedup bound sound.

**Frontend — verdict APPROVE-WITH-CHANGES.**
- *F1 (folded):* name the two `types.ts` interfaces. *F2 (folded):* `SchedulerSettingsCard`
  needs a second independent lead-days draft-state + two union widenings, not a shared draft.
  *F3 (folded):* pin CC lead-days min/max; resolve "0 = never fires" (kept `[0,31]`, 0 =
  disabled, billing parity). *M4/M6 (folded):* stale category counts in comments; widen
  existing mock fixtures. *M7 (folded):* verify email `returnTo` preserves `?edit`.
  Confirmed: `/accounts?edit=<id>` deep-link contract holds; no SWR hook exists here.

**Design/UX — verdict APPROVE-WITH-CHANGES.**
- *C1 (CRITICAL, folded):* backfill guard — suppress the close alert for cycles closing on/
  before account creation → D9. *I2 (folded):* per-card noise multiplier → D8 (reminder
  in-app only; one email stream per card). *I3 (folded):* `amount + " " + currency`, no `$`
  literal. *I4 (folded):* two labeled sub-sections with scoped labels in the settings card.
  *M1 (folded):* $0-due policy → D10. *M2/M3 (folded):* exact in-voice copy + category
  description. *M4 (folded):* deep-link destination is right; reconcile alert amount with the
  page figure. *M5 (noted):* "your [card]" broadcast to all members matches billing; may
  neutralize if it grates. *M6 (resolved, non-issue):* `payment_day` NULL is fine — the
  resolver defaults it, so `payment_date` is always computable.

**Security/Privacy — verdict APPROVE-WITH-CHANGES.**
- *F1 (HEADLINE, resolved — non-issue):* accounts have **no per-user owner** (org-scoped);
  every member already sees every balance, so notifying all members is **not** a new
  disclosure. Keep the all-members dispatch; do not invent owner-gating.
- *F2 (folded):* the dollar amount must not leave the trust boundary by email (billing emails
  carry only dates) → D8 (amount in-app only; email omits it). *F3 (folded):* keep the amount
  OUT of audit `detail`. *F5 (folded):* `_active_cc_accounts` + the ledger loader are required
  `org_id`-filter review checkpoints. *F6 (folded):* wire BOTH pref maps or the opt-out
  silently force-sends; settings write path stays `require_org_admin` + typed accessors; prefs
  are self-scoped. *F7 (folded):* dedup is one batched query per run (not per-card scan),
  balance math stays out of `is_due`, `JobResult.noop()` protects the rollout budget. *F4
  (resolved, non-issue):* deep-link is server-side org-scoped (404 on cross-org id).

## Cross-references

- `reference_scheduled_tasks_subsystem` — runner/toggle/audit model; the "CC-close deferred
  v2" note this discharges; the #516 first-tick cap.
- `reference_cc_model_v1` — `cc_cycle_service`, `cc_forecast_service`
  (`balance_at_close`/`outstanding_at_close`/`resolve_cycle_for_account`),
  `balance_contribution_filter` gotcha.
- `reference_abn_tab_import` — the native-MySQL-ENUM migration landmine (backend C1).
- `specs/2026-05-28-cc-billing-cycle.md` — the cycle substrate this rides on.
- `specs/2026-07-22-cc-model-v1-design.md` — the forecast integration reused for the amount.
