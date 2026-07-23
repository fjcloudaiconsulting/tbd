# CC Statement Alerts V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alert users before and at each credit-card statement close via two per-account scheduler jobs (pre-close reminder + close-day notification), with a dedicated opt-out notification category.

**Architecture:** Two org-granularity scheduler jobs fan out over each org's active credit-card accounts, resolving cycles via the existing `cc_cycle_service` and computing the statement balance via `cc_forecast_service`. Alert-only (no persisted statement). Dedup via audit rows. A new `cc_statement` notification category (migration 076, default ON) gates delivery; the reminder is in-app only and the close email omits the dollar amount.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2, MySQL 8 (SQLite for CI). Next.js 16, React 19, TypeScript, Vitest.

**Design spec:** `specs/2026-07-23-cc-statement-alerts-design.md` (architect-reviewed; read its Decisions table D1–D10 and Architect review resolutions before starting).

## Global Constraints

- **Isolated test stack only:** every `docker compose` / `docker exec` uses `-p team-ccalerts` (never the operator's default `pfv` project). Bring up once: `docker compose -p team-ccalerts up -d backend mysql redis`. Do NOT run `./pfv migrate`.
- **No AI attribution** in any commit or PR body. No `Co-Authored-By`, no "Generated with Claude".
- **Conventional-commit PR title / commit prefixes:** `type(scope): summary` (e.g. `feat(cc):`).
- **No em-dashes in user-facing copy.** Currency rendered as `amount + " " + currency` (e.g. `1,240.00 EUR`), never a `$` literal. Dates as `.isoformat()` (billing precedent).
- **Org-scoping:** every new query filters by `org_id`. The `_active_cc_accounts` query and the ledger loader are mandatory `org_id`-filter review checkpoints.
- **Migrations verified on real MySQL** (up/down/up), not just SQLite CI. Migration 076 alters a **native MySQL ENUM** — MySQL-guarded.
- **Preference default ON / opt-out:** new pref columns mirror `email_account` (`server_default="1"`, `default=True`), NOT `org_activity` (OFF).
- Tests run in-container: `docker compose -p team-ccalerts exec -T backend pytest <path>` and `docker compose -p team-ccalerts exec -T frontend npx vitest run <path>`.

---

### Task 1: Notification category `cc_statement` + migration 076 + preference plumbing

**Files:**
- Modify: `backend/app/models/notification.py` (enum member + 2 pref columns)
- Create: `backend/alembic/versions/076_cc_statement_notification_category.py`
- Modify: `backend/app/services/notification_service.py` (both pref maps + `_default_preferences`)
- Modify: `backend/app/schemas/notification.py` (`NotificationPreferencesResponse` + `NotificationPreferencesUpdate`)
- Test: `backend/tests/services/test_notification_cc_statement_prefs.py`, `backend/tests/migrations/test_migration_076.py` (or the repo's migration-test location)

**Interfaces:**
- Produces: `NotificationCategory.CC_STATEMENT` (value `"cc_statement"`); pref columns `email_cc_statement`, `in_app_cc_statement` (default True); map entries in `_IN_APP_PREF_FIELD`/`_EMAIL_PREF_FIELD`.

- [ ] **Step 1: Write the failing test — category is preference-gated both channels.**

```python
# backend/tests/services/test_notification_cc_statement_prefs.py
import pytest
from app.models.notification import NotificationCategory
from app.services import notification_service as ns

def test_cc_statement_category_exists():
    assert NotificationCategory.CC_STATEMENT.value == "cc_statement"

def test_cc_statement_wired_in_both_pref_maps():
    # Missing from either map silently force-sends (security F6). Both must be present.
    assert ns._IN_APP_PREF_FIELD[NotificationCategory.CC_STATEMENT] == "in_app_cc_statement"
    assert ns._EMAIL_PREF_FIELD[NotificationCategory.CC_STATEMENT] == "email_cc_statement"

@pytest.mark.asyncio
async def test_cc_statement_prefs_default_on(db_session, seed_user):
    prefs = await ns.get_preferences(db_session, user_id=seed_user.id)
    assert prefs.email_cc_statement is True
    assert prefs.in_app_cc_statement is True
```

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_notification_cc_statement_prefs.py -v`
Expected: FAIL (`AttributeError: CC_STATEMENT` / `KeyError`).

- [ ] **Step 3: Add the enum member + model columns.**

In `backend/app/models/notification.py`, add to `NotificationCategory`:
```python
    CC_STATEMENT = "cc_statement"
```
Add the two columns alongside the other pref columns (mirror `email_account`/`in_app_account` — default ON):
```python
    email_cc_statement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    in_app_cc_statement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
```

- [ ] **Step 4: Wire the pref maps + defaults in `notification_service.py`.**

```python
_IN_APP_PREF_FIELD[NotificationCategory.CC_STATEMENT] = "in_app_cc_statement"   # add to the dict literal
_EMAIL_PREF_FIELD[NotificationCategory.CC_STATEMENT] = "email_cc_statement"
```
(Add the entries inside the existing dict literals, not by mutation.) In `_default_preferences`, set `email_cc_statement=True, in_app_cc_statement=True`.

- [ ] **Step 5: Add both fields to the Pydantic schemas.**

In `backend/app/schemas/notification.py`, add `email_cc_statement: bool` and `in_app_cc_statement: bool` to BOTH `NotificationPreferencesResponse` and `NotificationPreferencesUpdate` (no default in Update — the whole-object PUT requires all fields).

- [ ] **Step 6: Write migration 076 (ENUM alter + 2 columns, MySQL-guarded).**

```python
# backend/alembic/versions/076_cc_statement_notification_category.py
"""cc statement notification category + prefs

Revision ID: 076_cc_statement_notification_category
Revises: 075_collapse_payment_strategy
"""
from alembic import op
import sqlalchemy as sa

revision = "076_cc_statement_notification_category"
down_revision = "075_collapse_payment_strategy"
branch_labels = None
depends_on = None

_OLD = "ENUM('security','account','org_admin','org_activity') NOT NULL"
_NEW = "ENUM('security','account','org_admin','org_activity','cc_statement') NOT NULL"

def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(f"ALTER TABLE notifications MODIFY COLUMN category {_NEW}")
    op.add_column(
        "user_notification_preferences",
        sa.Column("email_cc_statement", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "user_notification_preferences",
        sa.Column("in_app_cc_statement", sa.Boolean(), nullable=False, server_default="1"),
    )

def downgrade() -> None:
    bind = op.get_bind()
    op.drop_column("user_notification_preferences", "in_app_cc_statement")
    op.drop_column("user_notification_preferences", "email_cc_statement")
    if bind.dialect.name == "mysql":
        # Remap any rows using the new value before narrowing the ENUM, else the MODIFY fails.
        op.execute("UPDATE notifications SET category='org_activity' WHERE category='cc_statement'")
        op.execute(f"ALTER TABLE notifications MODIFY COLUMN category {_OLD}")
```
Confirm the exact table name for preferences (`user_notification_preferences`) and the `075` down_revision id string against `backend/alembic/versions/075_*.py`.

- [ ] **Step 7: Run the prefs tests — verify pass.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_notification_cc_statement_prefs.py -v`
Expected: PASS.

- [ ] **Step 8: Verify migration up/down/up on real MySQL.**

```bash
docker compose -p team-ccalerts exec -T backend alembic upgrade head
docker compose -p team-ccalerts exec -T backend python -c "import sqlalchemy as sa, os; \
e=sa.create_engine(os.environ['DATABASE_URL'].replace('+aiomysql','+pymysql')); \
print([r for r in e.connect().execute(sa.text(\"SHOW COLUMNS FROM notifications LIKE 'category'\"))])"
# Expect the ENUM to include 'cc_statement'.
docker compose -p team-ccalerts exec -T backend alembic downgrade -1
docker compose -p team-ccalerts exec -T backend alembic upgrade head
```
Expected: ENUM shows all five values after upgrade; downgrade then re-upgrade succeed without error.

- [ ] **Step 9: Commit.**

```bash
git add backend/app/models/notification.py backend/alembic/versions/076_*.py \
        backend/app/services/notification_service.py backend/app/schemas/notification.py \
        backend/tests/services/test_notification_cc_statement_prefs.py
git commit -m "feat(cc): add cc_statement notification category + prefs (migration 076)"
```

---

### Task 2: Extend `dispatch_notification_to_org_members` (in-app-only + divergent email body)

**Files:**
- Modify: `backend/app/services/notification_service.py:539` (`dispatch_notification_to_org_members`)
- Test: `backend/tests/services/test_dispatch_channel_control.py`

**Interfaces:**
- Produces: `dispatch_notification_to_org_members(..., send_email: bool = True, email_body: str | None = None)`. Existing callers unchanged (defaults preserve behavior). Email uses `email_body if email_body is not None else body`; email skipped when `send_email is False`.

- [ ] **Step 1: Write the failing test.**

```python
# backend/tests/services/test_dispatch_channel_control.py
import pytest
from unittest.mock import AsyncMock, patch
from app.models.notification import NotificationCategory
from app.services import notification_service as ns

@pytest.mark.asyncio
async def test_send_email_false_skips_email(db_session, seed_org_with_member):
    with patch.object(ns, "_send_notification_email_best_effort", new=AsyncMock()) as m:
        await ns.dispatch_notification_to_org_members(
            db_session, org_id=seed_org_with_member.id,
            category=NotificationCategory.CC_STATEMENT, event_type="t",
            title="x", body="y", send_email=False,
        )
        m.assert_not_awaited()

@pytest.mark.asyncio
async def test_email_body_overrides_body_for_email_only(db_session, seed_org_with_member):
    with patch.object(ns, "_send_notification_email_best_effort", new=AsyncMock()) as m:
        await ns.dispatch_notification_to_org_members(
            db_session, org_id=seed_org_with_member.id,
            category=NotificationCategory.CC_STATEMENT, event_type="t",
            title="x", body="in-app 100.00 EUR due", email_body="open the app",
        )
        assert m.await_args.kwargs["body"] == "open the app"
```
(Adapt `seed_org_with_member` to the repo's fixture for an org with one active user.)

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_dispatch_channel_control.py -v`
Expected: FAIL (unexpected `send_email`/`email_body` kwarg).

- [ ] **Step 3: Implement the extension.**

In `dispatch_notification_to_org_members` add `send_email: bool = True, email_body: Optional[str] = None` to the signature, and change the email call:
```python
        if send_email:
            await _send_notification_email_best_effort(
                db, user_id=uid, email=email, category=category, event_type=event_type,
                title=title, body=(email_body if email_body is not None else body), link_url=link_url,
            )
```

- [ ] **Step 4: Run to verify pass + no existing-caller regressions.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_dispatch_channel_control.py tests/services/test_notification_service.py -v`
Expected: PASS (existing dispatch tests still green).

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/notification_service.py backend/tests/services/test_dispatch_channel_control.py
git commit -m "feat(notifications): per-send email suppression + divergent email body"
```

---

### Task 3: Org-batched CC ledger loader + statement-outstanding helper

**Files:**
- Modify: `backend/app/services/cc_forecast_service.py` (add `load_cc_ledgers`, `statement_outstanding_at_close`) OR a new `backend/app/services/cc_statement_service.py` if `cc_forecast_service` should stay pure-math; follow the existing separation (ledger query currently lives inline in `account_balance_forecast_service.py:136-148`). Recommended: put the DB loader in `cc_statement_service.py`, keep math in `cc_forecast_service`.
- Test: `backend/tests/services/test_cc_statement_amount.py`

**Interfaces:**
- Produces:
  - `async def load_cc_ledgers(db, org_id: int, account_ids: list[int], up_to: date) -> dict[int, list[tuple[date, Decimal]]]` — signed cash-basis ledger per account, `balance_contribution_filter()`, no status clause, filtered by `org_id`.
  - `async def statement_outstanding(db, org_id, account, close_date) -> Decimal` — `outstanding_at_close(balance_at_close(Decimal(str(account.opening_balance)), ledger, close_date))`.

- [ ] **Step 1: Write the failing test (amount matches forecast semantics; grace period does not inflate).**

```python
# backend/tests/services/test_cc_statement_amount.py
import pytest
from datetime import date
from decimal import Decimal
from app.services import cc_statement_service as css

@pytest.mark.asyncio
async def test_statement_outstanding_matches_as_of_close(db_session, seed_cc_with_ledger):
    # seed: CC with opening_balance 0; a -100 purchase eff before close; a -30 purchase eff AFTER close.
    acct, close_date = seed_cc_with_ledger
    owed = await css.statement_outstanding(db_session, org_id=acct.org_id, account=acct, close_date=close_date)
    assert owed == Decimal("100.00")   # the +after-close 30 is excluded

@pytest.mark.asyncio
async def test_zero_when_paid_off(db_session, seed_cc_paid_off):
    acct, close_date = seed_cc_paid_off
    owed = await css.statement_outstanding(db_session, org_id=acct.org_id, account=acct, close_date=close_date)
    assert owed == Decimal("0")
```

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_cc_statement_amount.py -v`
Expected: FAIL (module/functions missing).

- [ ] **Step 3: Implement the loader + helper.**

Extract the ledger query verbatim from `account_balance_forecast_service.py:136-148` (the `case`-signed amount, `effective_period_date_expr()`, `balance_contribution_filter()`, join, `Transaction.org_id == org_id`, `account_id IN account_ids`, `effective_date <= up_to`), grouping results into `dict[account_id, list[(eff_date, signed_decimal)]]`. Then:
```python
from app.services import cc_forecast_service as ccf
async def statement_outstanding(db, org_id, account, close_date):
    ledgers = await load_cc_ledgers(db, org_id, [account.id], close_date)
    b_k = ccf.balance_at_close(Decimal(str(account.opening_balance)), ledgers.get(account.id, []), close_date)
    return ccf.outstanding_at_close(b_k)
```
Confirm the exact `balance_at_close` ledger-element shape (list of `(eff_date, signed)` vs objects) against `cc_forecast_service.py:67-90` and match it.

- [ ] **Step 4: Run to verify pass.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_cc_statement_amount.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/cc_statement_service.py backend/tests/services/test_cc_statement_amount.py
git commit -m "feat(cc): org-batched cc ledger loader + statement-outstanding helper"
```

---

### Task 4: Scheduler org-settings (toggle + clamped lead-days) + settings endpoint

**Files:**
- Modify: `backend/app/services/scheduler/org_settings.py`
- Modify: `backend/app/routers/scheduler.py` (GET/PUT include new fields) + the `SchedulerSettingsUpdate` schema (bound the field)
- Test: `backend/tests/services/test_scheduler_cc_settings.py`

**Interfaces:**
- Produces: `AUTOMATE_CC_STATEMENT_KEY="scheduler.automate_cc_statement_alerts"`, `CC_STATEMENT_REMINDER_LEAD_DAYS_KEY`, `get_cc_statement_lead_days(db, org_id) -> int` (clamped [0,31], default 2); `get_all()` returns `automate_cc_statement_alerts` + `cc_statement_reminder_lead_days`.

- [ ] **Step 1: Write the failing test.**

```python
# backend/tests/services/test_scheduler_cc_settings.py
import pytest
from app.services.scheduler import org_settings as so

@pytest.mark.asyncio
async def test_cc_toggle_defaults_on(db_session, seed_org):
    assert await so.get_bool(db_session, seed_org.id, so.AUTOMATE_CC_STATEMENT_KEY) is True

@pytest.mark.asyncio
async def test_cc_lead_days_default_and_clamp(db_session, seed_org):
    assert await so.get_cc_statement_lead_days(db_session, seed_org.id) == 2
    await so.set_value(db_session, seed_org.id, so.CC_STATEMENT_REMINDER_LEAD_DAYS_KEY, "99")
    assert await so.get_cc_statement_lead_days(db_session, seed_org.id) == 31  # clamped
```

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_scheduler_cc_settings.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement org-settings additions.**

Add the keys, `_BOOL_DEFAULTS[AUTOMATE_CC_STATEMENT_KEY]="true"`, a `_CC_STATEMENT_LEAD_DEFAULT=2` + `_CC_MIN,_CC_MAX=0,31`, `get_cc_statement_lead_days` (read `CC_STATEMENT_REMINDER_LEAD_DAYS_KEY`, int-parse, clamp — mirror `get_reminder_lead_days` at `org_settings.py:27-33`), and both keys in `get_all()`.

- [ ] **Step 4: Extend the scheduler settings router + schema.**

In `backend/app/routers/scheduler.py`, add `automate_cc_statement_alerts` (bool) + `cc_statement_reminder_lead_days` (int, `Field(ge=0, le=31)`) to `SchedulerSettingsUpdate`, and write them via the typed `so.set_value` accessors in the PUT handler (still under `require_org_admin`); include both in the GET response.

- [ ] **Step 5: Write + run an endpoint round-trip test.**

```python
@pytest.mark.asyncio
async def test_settings_endpoint_round_trips_cc_fields(admin_client):
    r = await admin_client.put("/api/v1/scheduler/settings", json={
        "automate_recurring_generation": True, "automate_billing_close": True,
        "billing_close_reminder_lead_days": 3,
        "automate_cc_statement_alerts": False, "cc_statement_reminder_lead_days": 5,
    })
    assert r.status_code == 200
    body = (await admin_client.get("/api/v1/scheduler/settings")).json()
    assert body["automate_cc_statement_alerts"] is False
    assert body["cc_statement_reminder_lead_days"] == 5
```
(Match the real PUT payload shape — include all existing required fields.)

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_scheduler_cc_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add backend/app/services/scheduler/org_settings.py backend/app/routers/scheduler.py \
        backend/tests/services/test_scheduler_cc_settings.py
git commit -m "feat(scheduler): per-org cc statement alert toggle + lead-days setting"
```

---

### Task 5: CC alert dedup audit helpers (batched)

**Files:**
- Modify: `backend/app/services/scheduler/audit.py`
- Test: `backend/tests/services/test_cc_alert_dedup.py`

**Interfaces:**
- Produces:
  - `CC_REMINDER_EVENT_TYPE="scheduler.cc_statement.reminder"`, `CC_CLOSED_EVENT_TYPE="scheduler.cc_statement.closed"`.
  - `async def record_cc_alert(*, org, account_id: int, close_date: date, event_type: str, detail: dict) -> int | None` (detail carries `account_id` + `close_date` ISO; NO amount).
  - `async def cc_alerts_sent_since(db, org_id: int, event_type: str, since: date) -> set[tuple[int, str]]` — set of `(account_id, close_date_iso)`.

- [ ] **Step 1: Write the failing test.**

```python
# backend/tests/services/test_cc_alert_dedup.py
import pytest
from datetime import date, timedelta
from app.services.scheduler import audit

@pytest.mark.asyncio
async def test_record_then_sent_set_contains_pair(db_session, seed_org):
    cd = date(2026, 7, 20)
    await audit.record_cc_alert(org=seed_org, account_id=7, close_date=cd,
                                event_type=audit.CC_CLOSED_EVENT_TYPE, detail={})
    sent = await audit.cc_alerts_sent_since(db_session, seed_org.id,
                                            audit.CC_CLOSED_EVENT_TYPE, date(2026, 7, 1))
    assert (7, "2026-07-20") in sent

@pytest.mark.asyncio
async def test_window_excludes_old(db_session, seed_org):
    sent = await audit.cc_alerts_sent_since(db_session, seed_org.id,
                                            audit.CC_CLOSED_EVENT_TYPE, date(2026, 7, 25))
    assert (7, "2026-07-20") not in sent
```
(`record_cc_alert` writes via its own session like `record_reminder`; the read uses `db_session`.)

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_cc_alert_dedup.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement (mirror `record_reminder`/`reminder_already_sent`).**

`record_cc_alert` writes an `AuditEvent` (own `async_session`, `actor_email="system"`, `target_org_id=org.id`) with `detail = {**detail, "account_id": account_id, "close_date": close_date.isoformat()}`. `cc_alerts_sent_since` selects `AuditEvent.event_type == event_type`, `target_org_id == org_id`, `AuditEvent.created_at >= since`, then builds `{(d["account_id"], d["close_date"]) for r in rows if (d:=r.detail or {}).get("account_id") is not None}`.

- [ ] **Step 4: Run to verify pass.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_cc_alert_dedup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/scheduler/audit.py backend/tests/services/test_cc_alert_dedup.py
git commit -m "feat(scheduler): batched cc-alert dedup audit helpers"
```

---

### Task 6: Notification templates (reminder + close, money-formatted)

**Files:**
- Modify: `backend/app/services/notification_templates.py`
- Test: `backend/tests/services/test_cc_statement_templates.py`

**Interfaces:**
- Produces:
  - `scheduler_cc_statement_reminder(card_name, close_date, days_until) -> tuple[str, str, str]` → `(title, body, link)`.
  - `scheduler_cc_statement_closed(card_name, amount_str, currency, payment_date, account_id) -> tuple[str, str, str, str]` → `(title, in_app_body, email_body, link)`; when `amount_str is None` (zero due) the in-app body is the "nothing due" variant.

- [ ] **Step 1: Write the failing test (copy shape, currency format, no em-dash).**

```python
# backend/tests/services/test_cc_statement_templates.py
from datetime import date
from app.services import notification_templates as t

def test_reminder_copy():
    title, body, link = t.scheduler_cc_statement_reminder("Amex Gold", date(2026,8,1), 2)
    assert title == "Amex Gold statement closes soon"
    assert "2026-08-01" in body and "2 day" in body and "—" not in body

def test_close_copy_amount_in_app_not_email():
    title, in_app, email, link = t.scheduler_cc_statement_closed("Amex Gold", "1,240.00", "EUR", date(2026,8,1), 42)
    assert title == "Amex Gold statement closed"
    assert "1,240.00 EUR is due on 2026-08-01" in in_app
    assert "1,240.00" not in email and "Open the app" in email
    assert link == "/accounts?edit=42"

def test_close_zero_due_body():
    title, in_app, email, link = t.scheduler_cc_statement_closed("Amex Gold", None, "EUR", date(2026,8,1), 42)
    assert "nothing due" in in_app
```

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_cc_statement_templates.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the templates** exactly per the spec's "Notification copy" section (titles, bodies, `link=f"/accounts?edit={account_id}"`).

- [ ] **Step 4: Run to verify pass.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/test_cc_statement_templates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/notification_templates.py backend/tests/services/test_cc_statement_templates.py
git commit -m "feat(cc): cc statement reminder + close notification templates"
```

---

### Task 7: Shared CC-alert helpers (active accounts, most-recent-closed cycle + backfill guard)

**Files:**
- Create: `backend/app/services/scheduler/jobs/cc_statement_common.py`
- Test: `backend/tests/services/scheduler/test_cc_statement_common.py`

**Interfaces:**
- Produces:
  - `async def active_cc_accounts(db, org_id) -> list[Account]` — `is_active` + `AccountType.slug=="credit_card"` + `close_day IS NOT NULL`, `org_id`-scoped.
  - `def most_recent_closed_cycle(account, today) -> CreditCardCycle | None` — anchors on `period_start-1 day`; returns `None` when `period_end_inclusive <= account.created_at.date()` (backfill guard, D9).

- [ ] **Step 1: Write the failing tests (cycle correctness + backfill guard).**

```python
# backend/tests/services/scheduler/test_cc_statement_common.py
from datetime import date
from types import SimpleNamespace
from app.services.scheduler.jobs import cc_statement_common as c

def _cc(close_day=20, created=date(2020,1,1)):
    return SimpleNamespace(close_day=close_day, payment_day=None,
                           payment_day_relative_month=None, created_at=SimpleNamespace(date=lambda: created))

def test_on_close_day_is_that_cycle():
    cyc = c.most_recent_closed_cycle(_cc(20), date(2026,7,20))
    assert cyc.period_end_inclusive == date(2026,7,20)

def test_twenty_days_past_close_resolves_prior_cycle():
    cyc = c.most_recent_closed_cycle(_cc(20), date(2026,8,9))   # 20 days past Jul 20 close
    assert cyc.period_end_inclusive == date(2026,7,20)

def test_backfill_guard_suppresses_precreation_cycle():
    # card created 2026-07-23, close_day 20 → most recent close 2026-07-20 predates creation.
    assert c.most_recent_closed_cycle(_cc(20, created=date(2026,7,23)), date(2026,7,24)) is None
```

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/scheduler/test_cc_statement_common.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement per the spec's "Close-cycle resolution + backfill guard" pseudocode**, using `resolve_cycle_for_account` from `cc_cycle_service`. `active_cc_accounts` mirrors the account+type join in `account_balance_forecast_service.py:70-76` with `Account.is_active.is_(True)` and `Account.close_day.isnot(None)`, scoped to `org_id`. (Confirm `account.created_at` exists; if the model exposes `opening_balance_date`, prefer it as the epoch.)

- [ ] **Step 4: Run to verify pass.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/scheduler/test_cc_statement_common.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/scheduler/jobs/cc_statement_common.py backend/tests/services/scheduler/test_cc_statement_common.py
git commit -m "feat(cc): shared cc-alert helpers (active accounts, closed-cycle + backfill guard)"
```

---

### Task 8: `CcStatementReminderJob` (in-app only) + registry

**Files:**
- Create: `backend/app/services/scheduler/jobs/cc_statement_reminder.py`
- Modify: `backend/app/services/scheduler/runner.py` (`REGISTRY`)
- Test: `backend/tests/services/scheduler/test_cc_statement_reminder_job.py`

**Interfaces:**
- Consumes: `active_cc_accounts`, `cc_alerts_sent_since`/`record_cc_alert` (`CC_REMINDER_EVENT_TYPE`), `resolve_cycle_for_account`, `get_cc_statement_lead_days`, `scheduler_cc_statement_reminder`, `dispatch_notification_to_org_members(..., send_email=False)`.
- Produces: `CcStatementReminderJob` (`job_type="cc_statement_reminder"`, `setting_key=AUTOMATE_CC_STATEMENT_KEY`).

- [ ] **Step 1: Write the failing tests.**

```python
# key cases (monkeypatch collaborators by module path, SQLite in-memory idiom):
# - is_due True when a card's close is `lead` days away and unsent; False at lead+1 or if already sent
# - run() dispatches in-app only (send_email=False), writes CC_REMINDER marker, commits
# - off-toggle short-circuits (job skipped by runner via setting_key)
# - second run same cycle is a no-op (dedup); JobResult.noop() when nothing dispatched
# - a card raising mid-run triggers db.rollback() and the other card still alerts
```
Write concrete assertions mirroring `backend/tests/services/scheduler/test_billing_reminder_job.py` (its fixtures + monkeypatch pattern) adapted to per-account fan-out.

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/scheduler/test_cc_statement_reminder_job.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the job** per the spec's "Two jobs" + "Data flow": `is_due` loads `active_cc_accounts`, fetches the sent-set once via `cc_alerts_sent_since(db, org.id, CC_REMINDER_EVENT_TYPE, today - timedelta(days=40))`, returns True if any card has `0 < (cycle.period_end_inclusive - today).days <= lead` and `(id, close_iso)` not in the set. `run` iterates due cards, per-card `try/except` with `await db.rollback()` on error + per-card failure audit; for each: `record_cc_alert` (marker first) → `dispatch_notification_to_org_members(..., category=CC_STATEMENT, send_email=False)` → `db.commit()`; return `JobResult.ok({...})` if any dispatched else `JobResult.noop()`. Append `CcStatementReminderJob()` to `REGISTRY`.

- [ ] **Step 4: Run to verify pass.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/scheduler/test_cc_statement_reminder_job.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/scheduler/jobs/cc_statement_reminder.py backend/app/services/scheduler/runner.py \
        backend/tests/services/scheduler/test_cc_statement_reminder_job.py
git commit -m "feat(scheduler): cc statement pre-close reminder job"
```

---

### Task 9: `CcStatementCloseJob` (in-app amount + amount-less email, $0 policy) + registry

**Files:**
- Create: `backend/app/services/scheduler/jobs/cc_statement_close.py`
- Modify: `backend/app/services/scheduler/runner.py` (`REGISTRY`)
- Test: `backend/tests/services/scheduler/test_cc_statement_close_job.py`

**Interfaces:**
- Consumes: `active_cc_accounts`, `most_recent_closed_cycle`, `statement_outstanding`, `cc_alerts_sent_since`/`record_cc_alert` (`CC_CLOSED_EVENT_TYPE`), `scheduler_cc_statement_closed`, `dispatch_notification_to_org_members(..., send_email=<bool>, email_body=<str>)`, a money formatter.
- Produces: `CcStatementCloseJob` (`job_type="cc_statement_closed"`, `setting_key=AUTOMATE_CC_STATEMENT_KEY`).

- [ ] **Step 1: Write the failing tests.**

```python
# key cases:
# - is_due True when most_recent_closed_cycle is not None, closed on/before today, unsent
# - backfill guard: card created after its last close → not due (no alert)
# - run() close alert: in-app body has "<amt> <ccy> is due on <date>", email_body omits the amount, send_email=True
# - $0 outstanding: send_email=False, in-app "nothing due", marker still written
# - dedup second tick no-op; per-card failure rollback + continue; JobResult.noop() when nothing dispatched
```
Adapt the `test_billing_close_job.py` fixtures; seed a CC with a ledger so `statement_outstanding` returns a known amount, and a paid-off CC for the $0 branch.

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/scheduler/test_cc_statement_close_job.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the job.** `is_due` loads active cards + the `CC_CLOSED_EVENT_TYPE` sent-set (40-day window); a card is due when `most_recent_closed_cycle(account, today)` is not None and its `(id, close_iso)` not in the set. `run` per due card (own `try/except` + `await db.rollback()` on error): compute `owed = statement_outstanding(...)`; `amount_str = format_money(owed, account.currency) if owed > 0 else None`; build copy via `scheduler_cc_statement_closed`; `record_cc_alert` (marker first); `dispatch_notification_to_org_members(category=CC_STATEMENT, title=title, body=in_app_body, email_body=email_body, link_url=link, send_email=(owed > 0))`; `db.commit()`. Return `JobResult.ok`/`noop`. Append `CcStatementCloseJob()` to `REGISTRY`. Use the money formatter from Task 6 / the codebase's existing amount formatter (confirm one exists backend-side; else format `f"{owed:,.2f}"`).

- [ ] **Step 4: Run to verify pass + full scheduler suite green.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services/scheduler/ -v`
Expected: PASS (all scheduler jobs, including the two new ones).

- [ ] **Step 5: Commit.**

```bash
git add backend/app/services/scheduler/jobs/cc_statement_close.py backend/app/services/scheduler/runner.py \
        backend/tests/services/scheduler/test_cc_statement_close_job.py
git commit -m "feat(scheduler): cc statement close-day alert job"
```

---

### Task 10: Frontend — notification preferences row + types

**Files:**
- Modify: `frontend/lib/types.ts` (`NotificationPreferences`)
- Modify: `frontend/app/settings/notifications/page.tsx` (CATEGORIES row + stale header comments)
- Modify: `frontend/tests/app/settings-notifications-page.test.tsx` (widen mock fixtures)
- Test: the same test file (add a case for the new row)

**Interfaces:**
- Consumes: backend prefs shape from Task 1 (`email_cc_statement`, `in_app_cc_statement`).

- [ ] **Step 1: Write the failing test.**

```tsx
// add to settings-notifications-page.test.tsx
it("renders the Credit card statements category with both toggles", async () => {
  // mock GET preferences returning the widened shape incl. email_cc_statement/in_app_cc_statement=true
  render(<NotificationsPage />);
  expect(await screen.findByText("Credit card statements")).toBeInTheDocument();
});
```
Also widen every existing mock preferences fixture in this file with `email_cc_statement: true, in_app_cc_statement: true` (required fields — omitting breaks the widened type).

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T frontend npx vitest run tests/app/settings-notifications-page.test.tsx`
Expected: FAIL (text not found / type error on fixtures).

- [ ] **Step 3: Implement.** Add `email_cc_statement: boolean; in_app_cc_statement: boolean;` to `NotificationPreferences` in `types.ts`. Add the CATEGORIES row (`id:"cc_statement"`, title `"Credit card statements"`, description per spec, `emailKey:"email_cc_statement"`, `inAppKey:"in_app_cc_statement"`, no `locked`). Update the stale header comments ("four notification categories"→five, "eight-field shape"→ten).

- [ ] **Step 4: Run + typecheck.**

Run: `docker compose -p team-ccalerts exec -T frontend npx vitest run tests/app/settings-notifications-page.test.tsx && docker compose -p team-ccalerts exec -T frontend npx tsc --noEmit`
Expected: PASS + no type errors.

- [ ] **Step 5: Commit.**

```bash
git add frontend/lib/types.ts frontend/app/settings/notifications/page.tsx frontend/tests/app/settings-notifications-page.test.tsx
git commit -m "feat(cc): credit card statements notification preferences row"
```

---

### Task 11: Frontend — SchedulerSettingsCard two sub-sections + second lead-days draft

**Files:**
- Modify: `frontend/lib/types.ts` (`SchedulerSettings`)
- Modify: `frontend/components/settings/SchedulerSettingsCard.tsx`
- Modify: `frontend/tests/scheduler-settings.test.tsx` (widen fixtures + new assertions)

**Interfaces:**
- Consumes: backend settings shape from Task 4 (`automate_cc_statement_alerts`, `cc_statement_reminder_lead_days`).

- [ ] **Step 1: Write the failing test.**

```tsx
// scheduler-settings.test.tsx — widen the mock SchedulerSettings with the two new fields, then:
it("renders a Credit-card statements sub-section with its own toggle + lead days", async () => {
  render(<SchedulerSettingsCard .../>);
  expect(await screen.findByText(/Credit-card statement alerts/i)).toBeInTheDocument();
});
it("persists the cc lead-days independently of the budget lead-days", async () => {
  // change the CC lead-days input, save, assert updateSchedulerSettings called with cc_statement_reminder_lead_days
  // and unchanged billing_close_reminder_lead_days
});
```

- [ ] **Step 2: Run to verify it fails.**

Run: `docker compose -p team-ccalerts exec -T frontend npx vitest run tests/scheduler-settings.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement per spec "Frontend" section.** Add the two fields to `SchedulerSettings` in `types.ts`. In `SchedulerSettingsCard.tsx`: add `ccLeadDaysDraft` state + a second commit handler (seed in the mount effect), widen `BooleanField` (`+"automate_cc_statement_alerts"`) and `savingField` (`+"cc_statement_reminder_lead_days"`), restructure into two labeled sub-sections ("Budget period" / "Credit-card statements") with the scoped labels + hints from the spec (CC lead-days min 0 max 31, "0 to 31 days" hint). Do NOT share the billing draft state.

- [ ] **Step 4: Run + typecheck + eslint.**

Run: `docker compose -p team-ccalerts exec -T frontend npx vitest run tests/scheduler-settings.test.tsx && docker compose -p team-ccalerts exec -T frontend npx tsc --noEmit && docker compose -p team-ccalerts exec -T frontend npx eslint . --quiet`
Expected: PASS + clean.

- [ ] **Step 5: Commit.**

```bash
git add frontend/lib/types.ts frontend/components/settings/SchedulerSettingsCard.tsx frontend/tests/scheduler-settings.test.tsx
git commit -m "feat(cc): scheduler settings card credit-card statements sub-section"
```

---

### Task 12: Full-suite verification + design-token/copy gates

**Files:** none (verification only).

- [ ] **Step 1: Backend full relevant suites.**

Run: `docker compose -p team-ccalerts exec -T backend pytest tests/services tests/routers -q`
Expected: all pass (no regressions from the dispatch extension or ENUM change).

- [ ] **Step 2: Frontend full suite + gates.**

Run: `docker compose -p team-ccalerts exec -T frontend npx vitest run && docker compose -p team-ccalerts exec -T frontend npx tsc --noEmit && docker compose -p team-ccalerts exec -T frontend npx eslint . --quiet && bash frontend/scripts/check-design-tokens.sh`
Expected: all pass; no off-token colors; no em-dashes.

- [ ] **Step 3: Migration head sanity on MySQL.**

Run: `docker compose -p team-ccalerts exec -T backend alembic upgrade head && docker compose -p team-ccalerts exec -T backend alembic current`
Expected: head is `076_cc_statement_notification_category`.

- [ ] **Step 4: No commit (verification task).** Proceed to whole-branch review, then open the PR.

---

## Self-Review

**Spec coverage:** D1 alert-only (no persist) — all tasks; D2 two moments — Tasks 8/9; D3 anchors — Tasks 7/8/9; D4 category+prefs — Tasks 1/10; D5 gate default ON — Task 4; D6 lead-days clamp — Task 4; D8 channels — Tasks 2/6/8/9; D9 backfill guard — Tasks 7/9; D10 $0 policy — Tasks 6/9. Backend C1 ENUM — Task 1; I2 defaults — Task 1; I3 marker-first — Tasks 8/9; I4 rollback — Tasks 8/9; I5 cycle anchor — Task 7; I6 org-batched loader — Task 3; M7/M8/M9/M10 — Tasks 1/7/8-9/4. Frontend F1/F2/F3 — Tasks 10/11. Security F2 amount-out-of-email — Tasks 2/6/9; F5 org-scoping — Tasks 3/7; F6 both maps — Task 1; F7 batched dedup + noop — Tasks 5/8/9. All covered.

**Placeholder scan:** implementation steps that reference "adapt the repo fixture" name the exact analog test file to mirror (`test_billing_reminder_job.py`, `test_billing_close_job.py`); no TBD/TODO in production steps.

**Type consistency:** `dispatch_notification_to_org_members(send_email, email_body)` (Task 2) is consumed identically in Tasks 8/9; `statement_outstanding`/`load_cc_ledgers` (Task 3) consumed in Task 9; `most_recent_closed_cycle`/`active_cc_accounts` (Task 7) consumed in Tasks 8/9; `CC_REMINDER_EVENT_TYPE`/`CC_CLOSED_EVENT_TYPE`/`cc_alerts_sent_since`/`record_cc_alert` (Task 5) consumed in Tasks 8/9; `email_cc_statement`/`in_app_cc_statement` consistent across Tasks 1/10; `automate_cc_statement_alerts`/`cc_statement_reminder_lead_days` consistent across Tasks 4/11. Consistent.
