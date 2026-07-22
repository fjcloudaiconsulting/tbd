# Credit Card Model V1 — Slice 2 (per-cycle payments store + endpoint + UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the per-cycle payment-amount store for credit-card accounts end-to-end — migration `074_cc_cycle_payments`, the `CcCyclePayment` ORM model, a thin `cc_cycle_payment_service` (amount/anchor validation + upcoming-cycle enumeration built on the shipped resolver), a new `cc_cycle_payments` router (`GET` upcoming collection = normal org read; `POST`/`PUT`/`DELETE {year}/{month}` = owner/admin only), post-commit `account.cycle_payment.*` audit events, org-wipe/reset deletes, the leave-CC delete cascade, and the inline "Upcoming payments" mini-list in the accounts edit block. NO forecast synthesis (Slice 3).

**Architecture:** A dedicated child table `cc_cycle_payments` (FK `account_id` → `accounts.id` `ON DELETE CASCADE`, no `org_id` column) keeps `amount NOT NULL` and depends only on the shipped resolver. The **anchor** is the cycle's CLOSE month `(period_end_inclusive.year, period_end_inclusive.month)`. Org isolation is enforced at the router by loading the parent account under `current_user.org_id` (the universal `accounts.py` pattern); mutations additionally gate on `_is_admin_user` (money-bearing, mirrors opening-balance). All cycle math is delegated to `cc_cycle_service.resolve_cycle_for_account` (D8: callers never re-derive) — the service enumerates the next `N=3` upcoming cycles by walking `resolve → period_end_inclusive + 1 day → resolve` and maps a `(year, month)` anchor to its cycle via `resolve_cycle_for_account(account, date(year, month, 1))` (day 1 always resolves to the cycle closing that month). Audit events fire post-commit via `audit_service.record_audit_event` (its own session), mirroring `account.opening_balance.update`. The frontend fetches the GET collection into an inline expandable mini-list gated to `payment_strategy ∈ {minimum_only, custom_per_period}`, persisting each row on blur/Enter via `PUT`, and `DELETE` on empty.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Alembic, MySQL 8 (SQLite in-memory for unit tests via aiosqlite); Next.js 16 + React 19 + TypeScript, Vitest + Testing Library.

## Global Constraints

- Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Pydantic v2 / MySQL 8.
- Frontend: Next.js 16 + React 19 + TypeScript.
- API versioning: every route under `/api/v1`. The new router uses `APIRouter(prefix="/api/v1/accounts", tags=["cc-cycle-payments"])`.
- Org-scoped every query: `cc_cycle_payments` has NO `org_id` column, so isolation is by loading the parent account `WHERE Account.id == account_id AND Account.org_id == current_user.org_id` and 404 on miss. NEVER query `cc_cycle_payments` without first resolving the org-scoped parent account (GET) or scoping the delete by an `account_id IN (SELECT accounts.id WHERE org_id=...)` subquery (org-wipe reset path).
- No em-dashes in user copy: separators use the middle dot `·` (U+00B7).
- No off-token colors (CI-blocked): the mini-list uses `label`/`input` primitives from `lib/styles.ts` and a `btnLink`-style Clear; helper/empty copy uses `text-xs text-text-muted`. No status/accent colors.
- No AI attribution in commit messages or PR bodies.
- Migrations MUST be verified with `alembic upgrade head` (and `downgrade -1` + re-`upgrade`) against a real MySQL container — SQLite CI cannot catch DDL drift (index/FK-cover class of bug).
- The isolated compose project `team-ccm1` is ALREADY UP. Do NOT run `docker compose up`. Run backend tests with `docker compose -p team-ccm1 exec -T backend pytest ...` and frontend with `docker compose -p team-ccm1 exec -T frontend npm test -- ...`. Always pass `-T`.
- Slice 2 EXCLUDES all forecast work: do NOT touch `account_balance_forecast_service.py` or `forecast_service.py`, and do NOT synthesize any payment deltas.

> **Executor note:** line numbers below reflect the repo at plan-authoring time and may drift. Treat them as anchors, not guarantees — the TDD loop (failing test first) catches any drift. Re-locate by the quoted surrounding code if a line ref does not match. Where the plan names variables in existing functions (`type_result`, `actor_email`, `req_id`, `ip`, `session_factory`, `_batch_delete_by_pk`, `_seed_org`/`_seed_full_org` fields), READ the real code and adapt to the actual names.

---

## File Structure

| File | Create / Modify | Responsibility |
|---|---|---|
| `backend/alembic/versions/074_cc_cycle_payments.py` | Create | `op.create_table("cc_cycle_payments", ...)`; FK `account_id`→`accounts.id` `ON DELETE CASCADE`; `UNIQUE(account_id, period_anchor_year, period_anchor_month)`. `down_revision = "073_credit_card_model_v1"`. |
| `backend/app/models/cc_cycle_payment.py` | Create | `CcCyclePayment` ORM model (id, account_id, period_anchor_year/month `SmallInteger`, amount `Numeric(12,2)`, created_at/updated_at). |
| `backend/app/models/__init__.py` | Modify | Import + register `CcCyclePayment` in `__all__`. |
| `backend/app/services/cc_cycle_payment_service.py` | Create | `validate_cycle_payment`, `upcoming_cycles`, `resolve_anchor_cycle`, `N_UPCOMING_CYCLES` — built on `cc_cycle_service.resolve_cycle_for_account`. |
| `backend/app/routers/cc_cycle_payments.py` | Create | `GET` upcoming collection + `POST`/`PUT`/`DELETE {year}/{month}` + post-commit audit; inline Pydantic response/request models. |
| `backend/app/main.py` | Modify | Import + `include_router(cc_cycle_payments.router)`. |
| `backend/app/services/org_data_service.py` | Modify | Explicit `cc_cycle_payments` deletes in `wipe_org_data` (before accounts) and `reset_org_data` (subquery-scoped — no `org_id` column). |
| `backend/app/services/account_type_change_service.py` | Modify | Leave-CC else-branch: snapshot + DELETE the account's `cc_cycle_payments`; carry the snapshot on `TypeChangeResult.deleted_cycle_payments`. |
| `backend/app/routers/accounts.py` | Modify | Post-commit `account.cycle_payment.deleted` audit loop over `type_result.deleted_cycle_payments`. |
| `backend/tests/test_cc_cycle_payments.py` | Create | Model roundtrip, service unit tests, endpoint integration tests, leave-CC cascade regression. |
| `backend/tests/services/test_org_data_service.py` | Modify | Seed a `cc_cycle_payment` row; add `"cc_cycle_payments"` to both `expected_keys` sets + the wiped-model list; explicit deletion assertion. |
| `frontend/lib/types.ts` | Modify | Add `UpcomingCyclePayment` interface. |
| `frontend/app/accounts/page.tsx` | Modify | Fetch the GET collection on edit-open (gated); render the inline "Upcoming payments" mini-list; persist on blur/Enter via PUT, empty → DELETE. |
| `frontend/tests/app/accounts-cc-model.test.tsx` | Modify | Append a "Upcoming payments" describe block. |

Test seeding, fixtures, and the FastAPI app-override harness are copied verbatim from `backend/tests/test_account_payment_source.py`; the frontend harness is reused from the existing `accounts-cc-model.test.tsx`.

---

## Task 1: Migration 074 + `CcCyclePayment` model + roundtrip test

**Files:**
- Create: `backend/alembic/versions/074_cc_cycle_payments.py`
- Create: `backend/app/models/cc_cycle_payment.py`
- Modify: `backend/app/models/__init__.py`
- Create (stub): `backend/app/routers/cc_cycle_payments.py`
- Test: `backend/tests/test_cc_cycle_payments.py` (new; roundtrip test only in this task)

**Interfaces:**
- Produces: `CcCyclePayment` with `id: int`, `account_id: int`, `period_anchor_year: int`, `period_anchor_month: int`, `amount: Decimal`, `created_at/updated_at: datetime`; table `cc_cycle_payments` with `UNIQUE(account_id, period_anchor_year, period_anchor_month) name="uq_cc_cycle_payments_account_period"` and FK `account_id`→`accounts.id` `ON DELETE CASCADE`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_cc_cycle_payments.py`. Copy the harness block (imports, `session_factory`, `_seed_org`, `worlds`, `_make_app`, `_account_row`) verbatim from `backend/tests/test_account_payment_source.py`, then make these three harness edits:

1. Add to the model imports: `from app.models.cc_cycle_payment import CcCyclePayment` and `from app.models.account import PaymentStrategy`.
2. Add `from app.routers.cc_cycle_payments import router as cc_cycle_payments_router` below the `accounts_router` import.
3. In `_make_app`, after `app.include_router(accounts_router)` add `app.include_router(cc_cycle_payments_router)`.

Then append the roundtrip test:

```python
def test_cc_cycle_payment_roundtrips(session_factory, worlds):
    """A cc_cycle_payments row persists and reads back, anchored to the
    close month with a NOT NULL amount."""
    import asyncio

    a = worlds["a"]

    async def _write_and_read() -> CcCyclePayment:
        async with session_factory() as db:
            db.add(
                CcCyclePayment(
                    account_id=a["cc_id"],
                    period_anchor_year=2026,
                    period_anchor_month=8,
                    amount=Decimal("125.00"),
                )
            )
            await db.commit()
        async with session_factory() as db:
            return (
                await db.execute(
                    select(CcCyclePayment).where(
                        CcCyclePayment.account_id == a["cc_id"]
                    )
                )
            ).scalar_one()

    row = asyncio.get_event_loop().run_until_complete(_write_and_read())
    assert row.period_anchor_year == 2026
    assert row.period_anchor_month == 8
    assert row.amount == Decimal("125.00")
    assert row.created_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py::test_cc_cycle_payment_roundtrips -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.cc_cycle_payment'` (and for `app.routers.cc_cycle_payments`).

- [ ] **Step 3: Create the model**

Create `backend/app/models/cc_cycle_payment.py`:

```python
"""Per-cycle credit-card payment amounts (Credit Card Model V1, Slice 2).

A dedicated child table for the amount the user plans to pay for a
given CC billing cycle (``specs/2026-07-22-cc-model-v1-design.md``
§ "Migration 074"). NOT an extension of the never-shipped
``cc_cycle_overrides``; depends only on the shipped
``cc_cycle_service`` resolver.

- Anchor = the cycle's CLOSE month
  (``period_end_inclusive.year`` / ``.month``). A Jan-25 close paid
  Feb-1 stores under ``(account, 2026, 1)``.
- No ``org_id`` column — org isolation is enforced at the router by
  loading the parent account under ``current_user.org_id``.
- ``ON DELETE CASCADE`` because a payment row is meaningless without
  its account; org-wipe/reset and the leave-CC path delete these
  rows explicitly anyway (defense in depth + accurate counts).
- ``amount`` is ``NOT NULL`` (no CHECK needed — a stored row always
  carries a real amount; "unset" is the absence of a row).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CcCyclePayment(Base):
    __tablename__ = "cc_cycle_payments"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "period_anchor_year",
            "period_anchor_month",
            name="uq_cc_cycle_payments_account_period",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_anchor_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period_anchor_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 4: Register the model**

In `backend/app/models/__init__.py`, add after the `from app.models.account import ...` line:

```python
from app.models.cc_cycle_payment import CcCyclePayment  # noqa: F401
```

and add `"CcCyclePayment",` to `__all__` (next to `"Account"`).

- [ ] **Step 5: Create the migration + router stub**

Create `backend/alembic/versions/074_cc_cycle_payments.py`:

```python
"""Create cc_cycle_payments table (Credit Card Model V1, Slice 2).

Revision ID: 074_cc_cycle_payments
Revises: 073_credit_card_model_v1
Create Date: 2026-07-22

Per-cycle CC payment amounts. Anchor = the cycle's CLOSE month
(period_anchor_year / period_anchor_month). No org_id column — org
isolation is enforced at the router by loading the parent account
under the caller's org_id. ``account_id`` FK to accounts.id ON DELETE
CASCADE (a payment row is meaningless without its account). ``amount``
NOT NULL, no CHECK.

Verified up/down on a real MySQL 8 container (isolated ``-p team-ccm1``
stack) — SQLite CI green does not prove MySQL DDL (index-length / FK-
cover class of bug).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "074_cc_cycle_payments"
down_revision: Union[str, None] = "073_credit_card_model_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cc_cycle_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("period_anchor_year", sa.SmallInteger(), nullable=False),
        sa.Column("period_anchor_month", sa.SmallInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_cc_cycle_payments_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "period_anchor_year",
            "period_anchor_month",
            name="uq_cc_cycle_payments_account_period",
        ),
    )


def downgrade() -> None:
    # Dropping the table drops its FK + unique index automatically.
    op.drop_table("cc_cycle_payments")
```

Also create `backend/app/routers/cc_cycle_payments.py` as a stub so the Step-1 harness import resolves (fleshed out in Task 3):

```python
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/accounts", tags=["cc-cycle-payments"])
```

- [ ] **Step 6: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py::test_cc_cycle_payment_roundtrips -v`
Expected: PASS.

- [ ] **Step 7: Verify the migration on real MySQL**

Run: `docker compose -p team-ccm1 exec -T backend alembic upgrade head` then `docker compose -p team-ccm1 exec -T backend alembic downgrade -1` then `docker compose -p team-ccm1 exec -T backend alembic upgrade head`
Expected: three clean runs, no DDL error; head reports `074_cc_cycle_payments`.

- [ ] **Step 8: Commit**

```bash
git add backend/alembic/versions/074_cc_cycle_payments.py backend/app/models/cc_cycle_payment.py backend/app/models/__init__.py backend/app/routers/cc_cycle_payments.py backend/tests/test_cc_cycle_payments.py
git commit -m "feat(accounts): add cc_cycle_payments table and model (migration 074)"
```

---

## Task 2: `cc_cycle_payment_service` — validation + cycle enumeration + unit tests

**Files:**
- Create: `backend/app/services/cc_cycle_payment_service.py`
- Test: `backend/tests/test_cc_cycle_payments.py` (append pure/service unit tests)

**Interfaces:**
- Consumes: `cc_cycle_service.resolve_cycle_for_account(account, target_date) -> CreditCardCycle(period_start, period_end_inclusive, payment_date, source)`.
- Produces:
  - `N_UPCOMING_CYCLES = 3`
  - `resolve_anchor_cycle(account, *, year, month) -> CreditCardCycle`
  - `upcoming_cycles(account, *, today, n=N_UPCOMING_CYCLES) -> list[CreditCardCycle]`
  - `validate_cycle_payment(*, account, account_slug, year, month, today, amount=None) -> None` — raises `HTTPException(422)` (non-CC / amount ≤ 0) or `HTTPException(409)` (past anchor).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_cc_cycle_payments.py`. Add imports: `from datetime import date`, `from fastapi import HTTPException`, and `from app.services import cc_cycle_payment_service as svc`. Add a DB-free stand-in:

```python
class _FakeAccount:
    """Minimal stand-in exposing the three resolver columns."""

    def __init__(self, *, close_day=None, payment_day=None, payment_day_relative_month=None):
        self.close_day = close_day
        self.payment_day = payment_day
        self.payment_day_relative_month = payment_day_relative_month


def test_upcoming_cycles_returns_three_distinct_forward_cycles():
    acct = _FakeAccount(close_day=15)
    today = date(2026, 7, 22)  # after the 15th -> current cycle closes Aug 15
    cycles = svc.upcoming_cycles(acct, today=today)
    assert len(cycles) == 3
    anchors = [(c.period_end_inclusive.year, c.period_end_inclusive.month) for c in cycles]
    assert anchors == [(2026, 8), (2026, 9), (2026, 10)]
    for c in cycles:
        assert c.period_end_inclusive < c.payment_date  # close before due


def test_resolve_anchor_cycle_maps_close_month():
    acct = _FakeAccount(close_day=15)
    cycle = svc.resolve_anchor_cycle(acct, year=2026, month=9)
    assert cycle.period_end_inclusive == date(2026, 9, 15)


def test_resolve_anchor_cycle_non_cc_raises():
    with pytest.raises(ValueError):
        svc.resolve_anchor_cycle(_FakeAccount(close_day=None), year=2026, month=9)


def test_validate_rejects_non_cc_422():
    acct = _FakeAccount(close_day=None)
    with pytest.raises(HTTPException) as exc:
        svc.validate_cycle_payment(
            account=acct, account_slug="checking",
            year=2026, month=9, today=date(2026, 7, 22),
            amount=Decimal("50.00"),
        )
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-1")])
def test_validate_rejects_non_positive_amount_422(bad_amount):
    acct = _FakeAccount(close_day=15)
    with pytest.raises(HTTPException) as exc:
        svc.validate_cycle_payment(
            account=acct, account_slug="credit_card",
            year=2026, month=9, today=date(2026, 7, 22),
            amount=bad_amount,
        )
    assert exc.value.status_code == 422


def test_validate_rejects_past_anchor_409():
    acct = _FakeAccount(close_day=15)
    with pytest.raises(HTTPException) as exc:
        svc.validate_cycle_payment(
            account=acct, account_slug="credit_card",
            year=2026, month=6, today=date(2026, 7, 22),
            amount=Decimal("50.00"),
        )
    assert exc.value.status_code == 409


def test_validate_accepts_current_and_future_anchor():
    acct = _FakeAccount(close_day=15)
    today = date(2026, 7, 22)  # current close month = Aug 2026
    svc.validate_cycle_payment(
        account=acct, account_slug="credit_card",
        year=2026, month=8, today=today, amount=Decimal("50.00"),
    )
    svc.validate_cycle_payment(
        account=acct, account_slug="credit_card",
        year=2027, month=1, today=today, amount=Decimal("50.00"),
    )


def test_validate_delete_path_skips_amount_check():
    acct = _FakeAccount(close_day=15)
    svc.validate_cycle_payment(
        account=acct, account_slug="credit_card",
        year=2026, month=8, today=date(2026, 7, 22), amount=None,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py -k "upcoming or resolve_anchor or validate" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.cc_cycle_payment_service'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/cc_cycle_payment_service.py`:

```python
"""Per-cycle CC payment validation + cycle enumeration (Slice 2).

Thin service over the shipped ``cc_cycle_service`` resolver
(``specs/2026-07-22-cc-model-v1-design.md`` § Validation, § Router
wiring). All cycle math is delegated to
``resolve_cycle_for_account`` (D8: callers never re-derive).

Rules (per spec):
  - Gate on ``slug == 'credit_card'`` ONLY (NOT on payment_strategy):
    amounts are stored regardless of the strategy in effect; the
    forecast reader (Slice 3) decides at read time whether to consult
    the table. Non-CC -> 422.
  - ``amount > 0`` -> else 422 (skipped when ``amount is None``, i.e.
    the DELETE path).
  - The (account, year, month) anchor must be CURRENT-or-FUTURE; a
    past-cycle write -> 409 (D6 read-only-past). "Current" = the close
    month of the cycle ``resolve_cycle_for_account(account, today)``
    falls in.

Anchor = the cycle's CLOSE month
(``period_end_inclusive.year`` / ``.month``). ``resolve_anchor_cycle``
resolves for day 1 of the anchor month: day 1 is always <= that month's
(clamped) close day, so the resolver returns exactly the cycle closing
in that month.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

from app.services.cc_cycle_service import CreditCardCycle, resolve_cycle_for_account


N_UPCOMING_CYCLES = 3
_CC = "credit_card"


def resolve_anchor_cycle(account: object, *, year: int, month: int) -> CreditCardCycle:
    """Map a close-month anchor ``(year, month)`` to its cycle.

    Resolves for day 1 of the anchor month. Because the close day is
    always >= 1, day 1 falls on-or-before that month's close, so the
    resolver returns the cycle whose ``period_end_inclusive`` is in
    ``(year, month)``. Raises ``ValueError`` on a non-CC account
    (``close_day is None``) — the resolver's own guard.
    """
    return resolve_cycle_for_account(account, date(year, month, 1))


def upcoming_cycles(
    account: object, *, today: date, n: int = N_UPCOMING_CYCLES
) -> list[CreditCardCycle]:
    """Return the next ``n`` cycles at/after ``today``.

    Walks forward: resolve the cycle for ``today`` (its
    ``period_end_inclusive`` is the next close on-or-after today), then
    step to the day after that close and resolve again.
    """
    cycles: list[CreditCardCycle] = []
    cursor = today
    for _ in range(n):
        cycle = resolve_cycle_for_account(account, cursor)
        cycles.append(cycle)
        cursor = cycle.period_end_inclusive + timedelta(days=1)
    return cycles


def _anchor_key(cycle: CreditCardCycle) -> tuple[int, int]:
    return (cycle.period_end_inclusive.year, cycle.period_end_inclusive.month)


def validate_cycle_payment(
    *,
    account: object,
    account_slug: Optional[str],
    year: int,
    month: int,
    today: date,
    amount: Optional[Decimal] = None,
) -> None:
    """Validate a per-cycle payment write against the spec rules.

    Raises ``HTTPException(422)`` for a non-CC account or a non-positive
    amount, ``HTTPException(409)`` for a past anchor. Returns ``None`` on
    success. ``amount=None`` (DELETE) skips the amount check but still
    enforces the CC gate + current-or-future rule.
    """
    if account_slug != _CC or getattr(account, "close_day", None) is None:
        raise HTTPException(
            status_code=422,
            detail="cycle payments are only allowed on credit_card accounts",
        )
    if amount is not None and amount <= 0:
        raise HTTPException(
            status_code=422,
            detail="amount must be greater than 0",
        )
    try:
        resolve_anchor_cycle(account, year=year, month=month)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="the requested cycle does not resolve to a credit_card cycle",
        )
    current = resolve_cycle_for_account(account, today)
    if (year, month) < _anchor_key(current):
        raise HTTPException(
            status_code=409,
            detail="cannot set a payment for a past cycle",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py -k "upcoming or resolve_anchor or validate" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/cc_cycle_payment_service.py backend/tests/test_cc_cycle_payments.py
git commit -m "feat(accounts): add cc_cycle_payment validation and cycle enumeration service"
```

---

## Task 3: `cc_cycle_payments` router — GET upcoming + POST/PUT/DELETE + audit + integration tests

**Files:**
- Modify (flesh out the Task-1 stub): `backend/app/routers/cc_cycle_payments.py`
- Modify: `backend/app/main.py` (import + `include_router`)
- Test: `backend/tests/test_cc_cycle_payments.py` (append endpoint integration tests)

**Interfaces:**
- Consumes: `get_db`, `get_current_user`, `get_session_factory`; `_is_admin_user`, `_request_id` (`app.routers.accounts`); `get_client_ip` (`app.rate_limit`); `cc_cycle_payment_service`; `audit_service.record_audit_event`.
- Produces: `GET …/cycle-payments` → `list[{year, month, close_date, due_date, amount|null}]` (next 3 cycles; normal org read; non-CC/no close_day → `[]`); `POST/PUT/DELETE …/{year}/{month}` (body `{amount}`), owner/admin only, with audit.

- [ ] **Step 1: Write the failing tests**

Append the endpoint integration tests to `backend/tests/test_cc_cycle_payments.py`:

```python
# ── endpoint: GET upcoming collection ───────────────────────────────────────


def test_get_upcoming_returns_three_cycles_with_dates(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert len(rows) == 3
    for r in rows:
        assert set(r.keys()) == {"year", "month", "close_date", "due_date", "amount"}
        assert r["amount"] is None
        assert r["close_date"] < r["due_date"]


def test_get_upcoming_non_cc_returns_empty(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{a['checking_id']}/cycle-payments")
    assert res.status_code == 200, res.text
    assert res.json() == []


def test_get_upcoming_cross_org_404(session_factory, worlds):
    a, b = worlds["a"], worlds["b"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{b['cc_id']}/cycle-payments")
    assert res.status_code == 404, res.text


def _first_upcoming_anchor(client, account_id) -> tuple[int, int]:
    rows = client.get(f"/api/v1/accounts/{account_id}/cycle-payments").json()
    return rows[0]["year"], rows[0]["month"]


def test_put_then_get_reflects_amount(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        put = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "200.00"},
        )
        assert put.status_code == 200, put.text
        rows = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments").json()
        hit = next(r for r in rows if r["year"] == year and r["month"] == month)
        assert hit["amount"] == "200.00"


def test_put_upsert_updates_existing(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "200.00"},
        )
        upd = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "250.00"},
        )
        assert upd.status_code == 200, upd.text
        rows = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments").json()
        hit = next(r for r in rows if r["year"] == year and r["month"] == month)
        assert hit["amount"] == "250.00"


def test_put_zero_amount_rejected_422(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "0"},
        )
    assert res.status_code == 422, res.text


def test_put_past_cycle_rejected_409(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/2000/1",
            json={"amount": "50.00"},
        )
    assert res.status_code == 409, res.text


def test_put_non_cc_rejected_422(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['checking_id']}/cycle-payments/2030/1",
            json={"amount": "50.00"},
        )
    assert res.status_code == 422, res.text


def test_delete_removes_row(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "200.00"},
        )
        res = client.delete(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}"
        )
        assert res.status_code == 200, res.text
        rows = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments").json()
        hit = next(r for r in rows if r["year"] == year and r["month"] == month)
        assert hit["amount"] is None


def test_delete_absent_404(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        res = client.delete(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}"
        )
    assert res.status_code == 404, res.text


def test_put_non_admin_forbidden(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["member_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "50.00"},
        )
    assert res.status_code == 403, res.text


def test_get_allowed_for_non_admin_member(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["member_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments")
    assert res.status_code == 200, res.text
    assert len(res.json()) == 3
```

> Harness addition: the copied `_seed_org` seeds only an admin. Add a non-admin member user (`role=Role.MEMBER`) in the same org and return `"member_id": member.id`. (`Role` is already imported.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py -k "get_upcoming or put or delete or admin or member" -v`
Expected: FAIL — the stub router has no routes (404 on every path).

- [ ] **Step 3: Write the router**

Replace `backend/app/routers/cc_cycle_payments.py` (the Task-1 stub) with:

```python
"""Per-cycle CC payment endpoints (Credit Card Model V1, Slice 2).

``specs/2026-07-22-cc-model-v1-design.md`` § "Router wiring / New
router". Collection feeds the "Upcoming payments" mini-list; the
{year}/{month} mutations are the close-month anchor.

Org isolation: the parent account is always loaded under
``current_user.org_id`` (the table has no org_id column). Reads are a
NORMAL org-scoped account read (any member); mutations are owner/admin
only (``_is_admin_user`` — money-bearing, mirrors opening-balance).
Audit events fire post-commit via ``record_audit_event`` (own session).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.account import Account
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.user import User
from app.rate_limit import get_client_ip
from app.routers.accounts import _is_admin_user, _request_id
from app.services import audit_service
from app.services import cc_cycle_payment_service as cycle_svc

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/accounts", tags=["cc-cycle-payments"])


class UpcomingCyclePaymentResponse(BaseModel):
    year: int
    month: int
    close_date: date
    due_date: date
    amount: Optional[Decimal] = None


class CyclePaymentWrite(BaseModel):
    amount: Decimal = Field(max_digits=12, decimal_places=2)


async def _load_account_or_404(
    db: AsyncSession, *, account_id: int, org_id: int
) -> Account:
    account = (
        await db.execute(
            select(Account)
            .options(selectinload(Account.account_type))
            .where(Account.id == account_id, Account.org_id == org_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _slug(account: Account) -> Optional[str]:
    return account.account_type.slug if account.account_type else None


@router.get(
    "/{account_id}/cycle-payments",
    response_model=list[UpcomingCyclePaymentResponse],
)
async def list_cycle_payments(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Next N=3 upcoming cycles for the CC, each with the stored amount
    (or null). Normal org-scoped read. Non-CC / no close_day -> []."""
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if _slug(account) != "credit_card" or account.close_day is None:
        return []

    rows = (
        await db.execute(
            select(CcCyclePayment).where(CcCyclePayment.account_id == account_id)
        )
    ).scalars().all()
    by_anchor = {
        (r.period_anchor_year, r.period_anchor_month): r.amount for r in rows
    }
    cycles = cycle_svc.upcoming_cycles(account, today=date.today())
    return [
        UpcomingCyclePaymentResponse(
            year=c.period_end_inclusive.year,
            month=c.period_end_inclusive.month,
            close_date=c.period_end_inclusive,
            due_date=c.payment_date,
            amount=by_anchor.get(
                (c.period_end_inclusive.year, c.period_end_inclusive.month)
            ),
        )
        for c in cycles
    ]


async def _audit_cycle_payment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    current_user: User,
    request: Request,
    account_id: int,
    year: int,
    month: int,
    detail_extra: dict,
) -> None:
    await audit_service.record_audit_event(
        session_factory,
        event_type=event_type,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"account_id": account_id, "year": year, "month": month, **detail_extra},
    )


@router.post("/{account_id}/cycle-payments/{year}/{month}")
async def create_cycle_payment(
    account_id: int,
    year: int,
    month: int,
    body: CyclePaymentWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    cycle_svc.validate_cycle_payment(
        account=account, account_slug=_slug(account),
        year=year, month=month, today=date.today(), amount=body.amount,
    )
    existing = (
        await db.execute(
            select(CcCyclePayment).where(
                CcCyclePayment.account_id == account_id,
                CcCyclePayment.period_anchor_year == year,
                CcCyclePayment.period_anchor_month == month,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="cycle payment already exists")

    db.add(
        CcCyclePayment(
            account_id=account_id,
            period_anchor_year=year,
            period_anchor_month=month,
            amount=body.amount,
        )
    )
    await db.commit()
    await _audit_cycle_payment(
        session_factory, event_type="account.cycle_payment.created",
        current_user=current_user, request=request,
        account_id=account_id, year=year, month=month,
        detail_extra={"amount": str(body.amount)},
    )
    return {"year": year, "month": month, "amount": str(body.amount)}


@router.put("/{account_id}/cycle-payments/{year}/{month}")
async def upsert_cycle_payment(
    account_id: int,
    year: int,
    month: int,
    body: CyclePaymentWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    cycle_svc.validate_cycle_payment(
        account=account, account_slug=_slug(account),
        year=year, month=month, today=date.today(), amount=body.amount,
    )
    row = (
        await db.execute(
            select(CcCyclePayment).where(
                CcCyclePayment.account_id == account_id,
                CcCyclePayment.period_anchor_year == year,
                CcCyclePayment.period_anchor_month == month,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        db.add(
            CcCyclePayment(
                account_id=account_id,
                period_anchor_year=year,
                period_anchor_month=month,
                amount=body.amount,
            )
        )
        await db.commit()
        await _audit_cycle_payment(
            session_factory, event_type="account.cycle_payment.created",
            current_user=current_user, request=request,
            account_id=account_id, year=year, month=month,
            detail_extra={"amount": str(body.amount)},
        )
    else:
        old_amount = row.amount
        row.amount = body.amount
        await db.commit()
        await _audit_cycle_payment(
            session_factory, event_type="account.cycle_payment.updated",
            current_user=current_user, request=request,
            account_id=account_id, year=year, month=month,
            detail_extra={"old_amount": str(old_amount), "amount": str(body.amount)},
        )
    return {"year": year, "month": month, "amount": str(body.amount)}


@router.delete("/{account_id}/cycle-payments/{year}/{month}")
async def delete_cycle_payment(
    account_id: int,
    year: int,
    month: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    row = (
        await db.execute(
            select(CcCyclePayment).where(
                CcCyclePayment.account_id == account_id,
                CcCyclePayment.period_anchor_year == year,
                CcCyclePayment.period_anchor_month == month,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="cycle payment not found")

    old_amount = row.amount
    await db.delete(row)
    await db.commit()
    await _audit_cycle_payment(
        session_factory, event_type="account.cycle_payment.deleted",
        current_user=current_user, request=request,
        account_id=account_id, year=year, month=month,
        detail_extra={"amount": str(old_amount)},
    )
    return {"year": year, "month": month, "deleted": True}
```

> DELETE does not call `validate_cycle_payment` (deleting a stale/past row must always succeed). Only POST/PUT enforce the anchor rule. Confirm `_is_admin_user` / `_request_id` are importable from `app.routers.accounts`; if the team forbids cross-router private imports, inline both helpers here (no behavior change).

- [ ] **Step 4: Register the router in main.py**

In `backend/app/main.py`, add `cc_cycle_payments` to the `from app.routers import ...` line, and after `app.include_router(accounts.router)` add:

```python
app.include_router(cc_cycle_payments.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py -v`
Expected: PASS (all endpoint + service + roundtrip tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/cc_cycle_payments.py backend/app/main.py backend/tests/test_cc_cycle_payments.py
git commit -m "feat(accounts): add cc cycle-payments endpoints with audit events"
```

---

## Task 4: Org-wipe / reset deletes + regression test

**Files:**
- Modify: `backend/app/services/org_data_service.py`
- Modify: `backend/tests/services/test_org_data_service.py`

**Interfaces:**
- Consumes: `CcCyclePayment` (Task 1); the existing `wipe_org_data` / `reset_org_data` order (children before parents; deletes before `accounts`).
- Produces: `counts["cc_cycle_payments"]` in both paths; org-scoped delete before the `accounts` delete.

- [ ] **Step 1: Write the failing test (and fix the existing exact-set assertions)**

In `backend/tests/services/test_org_data_service.py`:

1. Add: `from app.models.cc_cycle_payment import CcCyclePayment`.
2. In the full-org seed helper, after the `account` row commits (near the `import_batches` seed), add one `CcCyclePayment` row:

```python
        db.add(CcCyclePayment(
            account_id=account.id,
            period_anchor_year=2099,
            period_anchor_month=1,
            amount=Decimal("100.00"),
        ))
        await db.commit()
```

3. Add `"cc_cycle_payments",` to BOTH `expected_keys` sets (wipe test + reset test) — they assert exact set equality.
4. In the wipe test's wiped-model loop, add `CcCyclePayment` to the tuple of models asserted gone.
5. Append a regression test:

```python
@pytest.mark.asyncio
async def test_reset_wipes_cc_cycle_payments(session_factory):
    """reset_org_data must delete cc_cycle_payments via the subquery-
    scoped delete (no org_id column, so _batch_delete_by_pk is unusable)."""
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        before = await _count(db, CcCyclePayment)
        assert before >= 1

    async with session_factory() as db:
        counts = await org_data_service.reset_org_data(db, org_id=seeded["org_id"])

    assert counts["cc_cycle_payments"] >= 1
    async with session_factory() as db:
        assert await _count(db, CcCyclePayment) == 0
```

> Adapt the seed-helper name (`_seed_full_org`), the `_count` helper, and the `expected_keys`/wiped-model locations to whatever the real test file uses — read it first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_org_data_service.py -k "wipe or reset or cc_cycle" -v`
Expected: FAIL — `counts` has no `cc_cycle_payments` key / set-inequality on `expected_keys`.

- [ ] **Step 3: Write the implementation**

In `backend/app/services/org_data_service.py`, add `from app.models.cc_cycle_payment import CcCyclePayment` near the model imports.

In `wipe_org_data`, immediately BEFORE the `counts["accounts"]` delete, insert:

```python
    # cc_cycle_payments.account_id FKs accounts.id ON DELETE CASCADE, but
    # codebase convention deletes cascade children explicitly (accurate
    # counts + dialect-independent SQLite tests). No org_id column, so scope
    # by the org's account ids. Credit Card Model V1, Slice 2.
    counts["cc_cycle_payments"] = (
        await db.execute(
            delete(CcCyclePayment).where(
                CcCyclePayment.account_id.in_(
                    select(Account.id).where(Account.org_id == org_id)
                )
            )
        )
    ).rowcount or 0
```

In `reset_org_data`, immediately BEFORE the `counts["accounts"] = await _batch_delete_by_pk(...)` block, insert (NOT via `_batch_delete_by_pk` — it filters on `model.org_id`, which this table lacks):

```python
    # cc_cycle_payments has no org_id column, so _batch_delete_by_pk cannot
    # scope it. Use a single subquery-scoped delete before the accounts wipe.
    # Credit Card Model V1, Slice 2.
    counts["cc_cycle_payments"] = (
        await db.execute(
            delete(CcCyclePayment).where(
                CcCyclePayment.account_id.in_(
                    select(Account.id).where(Account.org_id == org_id)
                )
            )
        )
    ).rowcount or 0
```

> Match `reset_org_data`'s existing commit/transaction discipline — if the surrounding deletes each commit, mirror that; if they batch into one commit, do not add a stray commit. Read the function and follow its pattern.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_org_data_service.py -v`
Expected: PASS.

- [ ] **Step 5: Regression-check the admin-delete cascade path**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/routers/test_admin_orgs_delete.py tests/routers/test_org_data.py -q` (adjust to the real test paths if these differ)
Expected: PASS (`wipe_org_data` is shared by org-delete-cascade).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/org_data_service.py backend/tests/services/test_org_data_service.py
git commit -m "feat(accounts): wipe cc_cycle_payments on org wipe and reset"
```

---

## Task 5: Leave-CC delete cascade + audit + regression test

**Files:**
- Modify: `backend/app/services/account_type_change_service.py` (add `deleted_cycle_payments` to `TypeChangeResult`; snapshot + delete in the else-branch).
- Modify: `backend/app/routers/accounts.py` (post-commit `account.cycle_payment.deleted` audit loop).
- Test: `backend/tests/test_cc_cycle_payments.py` (append leave-CC regression).

**Interfaces:**
- Consumes: `CcCyclePayment` (Task 1); `apply_type_change_in_session` else-branch; the atomic PUT audit path.
- Produces: `TypeChangeResult.deleted_cycle_payments: list[dict]` (each `{"year", "month", "amount"}`); leaving `credit_card` deletes the account's `cc_cycle_payments` in the same atomic transaction and emits one `account.cycle_payment.deleted` per removed row post-commit.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cc_cycle_payments.py`:

```python
def test_leaving_cc_deletes_cycle_payments(session_factory, worlds):
    """Converting a CC to a non-CC type must delete its cc_cycle_payments
    rows (money-bearing rows anchored to a close_day being cleared)."""
    import asyncio

    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "175.00"},
        )
        conv = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"account_type_id": a["type_ids"]["checking"]},
        )
        assert conv.status_code == 200, conv.text

    async def _count_rows() -> int:
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(CcCyclePayment).where(
                        CcCyclePayment.account_id == a["cc_id"]
                    )
                )
            ).scalars().all()
            return len(rows)

    assert asyncio.get_event_loop().run_until_complete(_count_rows()) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py::test_leaving_cc_deletes_cycle_payments -v`
Expected: FAIL — the type-change path clears the columns but leaves the `cc_cycle_payments` rows (count == 1).

- [ ] **Step 3: Write the service change**

In `backend/app/services/account_type_change_service.py`: add `delete` to the sqlalchemy import and `from app.models.cc_cycle_payment import CcCyclePayment`.

Add `deleted_cycle_payments` to `TypeChangeResult` (`__slots__` + an `__init__` param defaulting to `None`, assigned as `self.deleted_cycle_payments = deleted_cycle_payments or []`).

In `apply_type_change_in_session`, initialize `deleted_cycle_payments: list = []` before the `if target_slug == _CC:` split, and in the `else:` branch (after `account.fixed_payment_amount = None`) insert:

```python
        # Credit Card Model V1 (Slice 2): per-cycle payment rows are money-
        # bearing and anchored to the close_day being cleared here. Keeping
        # them orphans money data no UI can surface and risks resurrecting
        # stale amounts on a later revert. ON DELETE CASCADE only covers
        # account DELETION, not a type change, so delete explicitly; snapshot
        # first so the router can emit account.cycle_payment.deleted events.
        _cp_rows = (
            await svc_db.execute(
                select(CcCyclePayment).where(
                    CcCyclePayment.account_id == account_id
                )
            )
        ).scalars().all()
        deleted_cycle_payments = [
            {"year": r.period_anchor_year, "month": r.period_anchor_month, "amount": str(r.amount)}
            for r in _cp_rows
        ]
        if _cp_rows:
            await svc_db.execute(
                delete(CcCyclePayment).where(CcCyclePayment.account_id == account_id)
            )
```

Pass `deleted_cycle_payments=deleted_cycle_payments` into the `TypeChangeResult(...)` construction.

> Use the real session variable name in `apply_type_change_in_session` (the plan assumes `svc_db`; confirm) and the real `account_id` binding.

- [ ] **Step 4: Write the router audit loop**

In `backend/app/routers/accounts.py`, inside the atomic update path, after the existing `account.type_changed` audit block (post-commit region), add a loop emitting one `account.cycle_payment.deleted` per `type_result.deleted_cycle_payments` entry, reusing the SAME actor/request-id/ip/session_factory variables the neighboring `account.type_changed` audit uses:

```python
    if type_result is not None and type_result.deleted_cycle_payments:
        for cp in type_result.deleted_cycle_payments:
            await audit_service.record_audit_event(
                session_factory,
                event_type="account.cycle_payment.deleted",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                target_org_id=actor_org_id,
                target_org_name=None,
                request_id=req_id,
                ip_address=ip,
                outcome="success",
                detail={"account_id": account_id, "year": cp["year"], "month": cp["month"], "amount": cp["amount"]},
            )
```

> Adapt every variable name (`actor_user_id`, `actor_email`, `actor_org_id`, `req_id`, `ip`, `session_factory`, `type_result`) to the actual locals in the neighboring type-changed audit block — copy its call shape exactly.

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py::test_leaving_cc_deletes_cycle_payments -v`
Expected: PASS.

- [ ] **Step 6: Regression-check the change-type + CC-fields suites**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/test_cc_cycle_payments.py tests/routers/test_accounts_change_type.py tests/test_account_credit_card_fields.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/account_type_change_service.py backend/app/routers/accounts.py backend/tests/test_cc_cycle_payments.py
git commit -m "feat(accounts): delete cc_cycle_payments and audit on leaving credit_card"
```

---

## Task 6: Frontend `UpcomingCyclePayment` type + GET wiring

**Files:**
- Modify: `frontend/lib/types.ts` (new interface).
- Modify: `frontend/app/accounts/page.tsx` (state + gated fetch effect + persist helper).

**Interfaces:**
- Consumes: `GET /api/v1/accounts/{id}/cycle-payments` → `UpcomingCyclePayment[]`.
- Produces: `UpcomingCyclePayment` interface; `upcomingCycles`/`cycleDrafts` state populated only when a CC row is being edited AND `payment_strategy ∈ {minimum_only, custom_per_period}`; `persistCycleAmount(year, month, raw)`.

- [ ] **Step 1: Implement the type**

In `frontend/lib/types.ts`, after the `Account` interface add:

```typescript
// Credit Card Model V1 (Slice 2). One upcoming CC billing cycle with the
// planned per-cycle payment amount (null when unset). Dates come from the
// backend resolver so the FE never re-derives cycle math. Decimals serialize
// as strings.
export interface UpcomingCyclePayment {
  year: number;
  month: number;
  close_date: string; // ISO date (YYYY-MM-DD)
  due_date: string; // ISO date (YYYY-MM-DD)
  amount: string | null;
}
```

- [ ] **Step 2: Implement state + gated fetch effect + persist helper**

In `frontend/app/accounts/page.tsx`: add `UpcomingCyclePayment` to the `@/lib/types` import; ensure `useEffect` is imported. Read the real names for the edited-account id and the edit-strategy state (`editAcctId`/`editAcctPaymentStrategy`/`editingTypeSlug`) and adapt. Add:

```typescript
  // Credit Card Model V1 (Slice 2) — upcoming per-cycle payments for the
  // edited CC. Populated only for minimum_only / custom_per_period.
  const [upcomingCycles, setUpcomingCycles] = useState<UpcomingCyclePayment[]>([]);
  const [cycleDrafts, setCycleDrafts] = useState<Record<string, string>>({});
```

Gated fetch effect (place after `editingTypeSlug` is defined):

```typescript
  // Fetch the upcoming-payments collection when a CC row is being edited
  // under a per-cycle strategy. Backend supplies the cycle windows.
  useEffect(() => {
    const perCycle =
      editAcctPaymentStrategy === "minimum_only" ||
      editAcctPaymentStrategy === "custom_per_period";
    if (editAcctId == null || editingTypeSlug !== "credit_card" || !perCycle) {
      setUpcomingCycles([]);
      setCycleDrafts({});
      return;
    }
    let cancelled = false;
    apiFetch<UpcomingCyclePayment[]>(`/api/v1/accounts/${editAcctId}/cycle-payments`)
      .then((rows) => {
        if (cancelled) return;
        setUpcomingCycles(rows);
        setCycleDrafts(
          Object.fromEntries(rows.map((r) => [`${r.year}-${r.month}`, r.amount ?? ""])),
        );
      })
      .catch(() => {
        if (!cancelled) {
          setUpcomingCycles([]);
          setCycleDrafts({});
        }
      });
    return () => {
      cancelled = true;
    };
  }, [editAcctId, editingTypeSlug, editAcctPaymentStrategy]);
```

Persist helper (near `_doSaveAcct`):

```typescript
  // Persist one cycle amount. Empty -> DELETE, else PUT. Re-fetches so the
  // Clear affordance and stored amounts stay in sync.
  async function persistCycleAmount(year: number, month: number, raw: string) {
    if (editAcctId == null) return;
    const value = raw.trim();
    const path = `/api/v1/accounts/${editAcctId}/cycle-payments/${year}/${month}`;
    try {
      if (value === "") {
        await apiFetch(path, { method: "DELETE" }).catch(() => {});
      } else {
        await apiFetch(path, { method: "PUT", body: JSON.stringify({ amount: value }) });
      }
      const rows = await apiFetch<UpcomingCyclePayment[]>(
        `/api/v1/accounts/${editAcctId}/cycle-payments`,
      );
      setUpcomingCycles(rows);
      setCycleDrafts(
        Object.fromEntries(rows.map((r) => [`${r.year}-${r.month}`, r.amount ?? ""])),
      );
    } catch (e) {
      setError(extractErrorMessage(e));
    }
  }
```

> Use the real error-setter / error-extractor the file already uses (`setError`/`extractErrorMessage` are assumed; adapt to actuals).

- [ ] **Step 3: Type-check**

Run: `docker compose -p team-ccm1 exec -T frontend npx tsc --noEmit`
Expected: no errors (the mini-list JSX arrives in Task 7).

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/types.ts frontend/app/accounts/page.tsx
git commit -m "feat(accounts): fetch upcoming cycle payments for the edited credit card"
```

---

## Task 7: Frontend "Upcoming payments" mini-list + tests

**Files:**
- Modify: `frontend/app/accounts/page.tsx` (render the gated mini-list in the CC edit block).
- Test: `frontend/tests/app/accounts-cc-model.test.tsx` (append a describe block).

**Interfaces:**
- Consumes: `upcomingCycles`, `cycleDrafts`, `persistCycleAmount` (Task 6); `label`/`input`/`btnLink` from `lib/styles.ts`.
- Produces: an inline expandable "Upcoming payments" mini-list gated to `editingTypeSlug === "credit_card" && payment_strategy ∈ {minimum_only, custom_per_period}`; each row shows `Closes {close_date} · due {due_date}`, an amount input (`placeholder="amount not set"`), and a `Clear` button when a saved amount exists; empty copy when no cycles.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/tests/app/accounts-cc-model.test.tsx`. Extend `mockApi` so the collection endpoint returns rows and mutations resolve (add before the trailing default return of the mock impl):

```tsx
    if (path.match(/\/api\/v1\/accounts\/\d+\/cycle-payments$/)) {
      return Promise.resolve([
        { year: 2026, month: 8, close_date: "2026-08-15", due_date: "2026-09-01", amount: null },
        { year: 2026, month: 9, close_date: "2026-09-15", due_date: "2026-10-01", amount: "120.00" },
        { year: 2026, month: 10, close_date: "2026-10-15", due_date: "2026-11-01", amount: null },
      ]);
    }
    if (path.match(/\/api\/v1\/accounts\/\d+\/cycle-payments\/\d+\/\d+$/)) {
      return Promise.resolve({});
    }
```

Then append:

```tsx
describe("CC Model — upcoming payments mini-list", () => {
  test("shows the section only under a per-cycle strategy", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11); // CC seeded with payment_strategy=minimum_only
    expect(await screen.findByText(/Upcoming payments/i)).toBeTruthy();
    expect(
      screen.getByText(/Enter what you plan to pay each cycle\. We use it in your forecast\./i),
    ).toBeTruthy();
    expect(screen.getByText(/Closes 2026-08-15 · due 2026-09-01/)).toBeTruthy();
  });

  test("hides the section for full_balance", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText(/Payment strategy/i), {
      target: { value: "full_balance" },
    });
    expect(screen.queryByText(/Upcoming payments/i)).toBeNull();
  });

  test("empty amount persists via DELETE, filled via PUT", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const firstInput = await screen.findByLabelText(/Planned payment for 2026-08/i);
    fireEvent.change(firstInput, { target: { value: "90" } });
    fireEvent.blur(firstInput);
    await waitFor(() => {
      const put = vi.mocked(apiFetch).mock.calls.find(
        ([p, init]) =>
          p === "/api/v1/accounts/11/cycle-payments/2026/8" && init?.method === "PUT",
      );
      expect(put).toBeTruthy();
      expect(JSON.parse(String(put![1]?.body)).amount).toBe("90");
    });
  });

  test("clearing a saved amount deletes it", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const clears = await screen.findAllByRole("button", { name: /^Clear$/ });
    fireEvent.click(clears[0]);
    await waitFor(() => {
      const del = vi.mocked(apiFetch).mock.calls.find(
        ([p, init]) =>
          p === "/api/v1/accounts/11/cycle-payments/2026/9" && init?.method === "DELETE",
      );
      expect(del).toBeTruthy();
    });
  });
});
```

> The seeded `CC` fixture must have `payment_strategy: "minimum_only"` for the section to show by default (Slice 1 set it to `minimum_only` already; confirm).

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec -T frontend npm test -- tests/app/accounts-cc-model.test.tsx -t "upcoming payments"`
Expected: FAIL — section text / row labels not found.

- [ ] **Step 3: Write the mini-list JSX**

In `frontend/app/accounts/page.tsx`, inside the CC edit block, immediately AFTER the conditional Fixed-payment field block, insert:

```tsx
                    {/* Credit Card Model V1 (Slice 2) — inline "Upcoming
                        payments" mini-list. Gated to the per-cycle
                        strategies; cycle windows come from the backend
                        (FE never re-derives). Persist on blur/Enter via
                        PUT; empty clears via DELETE. Middle dot separator,
                        no em-dash; muted design tokens only. */}
                    {editingTypeSlug === "credit_card" &&
                      (editAcctPaymentStrategy === "minimum_only" ||
                        editAcctPaymentStrategy === "custom_per_period") && (
                        <div className="w-full">
                          <div className={label}>Upcoming payments</div>
                          <p className="mb-2 text-xs text-text-muted">
                            Enter what you plan to pay each cycle. We use it in your forecast.
                          </p>
                          {upcomingCycles.length === 0 ? (
                            <p className="text-xs text-text-muted">
                              No upcoming cycles yet. Set a bill close day first.
                            </p>
                          ) : (
                            <ul className="flex flex-col gap-2">
                              {upcomingCycles.map((c) => {
                                const key = `${c.year}-${c.month}`;
                                const monthKey = `${c.year}-${String(c.month).padStart(2, "0")}`;
                                return (
                                  <li
                                    key={key}
                                    className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-3"
                                  >
                                    <span className="text-xs tabular-nums text-text-muted sm:w-56">
                                      Closes {c.close_date} · due {c.due_date}
                                    </span>
                                    <input
                                      type="number"
                                      step="0.01"
                                      min={0}
                                      aria-label={`Planned payment for ${monthKey}`}
                                      value={cycleDrafts[key] ?? ""}
                                      placeholder="amount not set"
                                      onChange={(e) =>
                                        setCycleDrafts((d) => ({ ...d, [key]: e.target.value }))
                                      }
                                      onBlur={() =>
                                        persistCycleAmount(c.year, c.month, cycleDrafts[key] ?? "")
                                      }
                                      onKeyDown={(e) => {
                                        if (e.key === "Enter") {
                                          e.preventDefault();
                                          persistCycleAmount(c.year, c.month, cycleDrafts[key] ?? "");
                                        }
                                      }}
                                      className={`w-full text-sm sm:w-32 ${input}`}
                                    />
                                    {c.amount != null && (
                                      <button
                                        type="button"
                                        className={btnLink}
                                        onClick={() => persistCycleAmount(c.year, c.month, "")}
                                      >
                                        Clear
                                      </button>
                                    )}
                                  </li>
                                );
                              })}
                            </ul>
                          )}
                        </div>
                      )}
```

Ensure `btnLink` is in the `@/lib/styles` import list (add if absent). If `btnLink` does not exist in `lib/styles.ts`, use the existing link-style class the file already uses for text buttons.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec -T frontend npm test -- tests/app/accounts-cc-model.test.tsx`
Expected: PASS (all describe blocks, including the Slice-1 form/subline blocks).

- [ ] **Step 5: Type-check + lint + token check + accounts-page regression**

Run: `docker compose -p team-ccm1 exec -T frontend npx tsc --noEmit` then `docker compose -p team-ccm1 exec -T frontend npx eslint app/accounts/page.tsx lib/types.ts tests/app/accounts-cc-model.test.tsx --quiet` then the design-token check `frontend/scripts/check-design-tokens.sh` if present, then `docker compose -p team-ccm1 exec -T frontend npm test -- tests/app/accounts-payment-source.test.tsx tests/app/accounts-edit-type.test.tsx`
Expected: no type/lint/token errors; existing accounts-page suites PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/accounts/page.tsx frontend/tests/app/accounts-cc-model.test.tsx
git commit -m "feat(accounts): add upcoming payments mini-list to the credit card editor"
```

---

## Self-review notes

**Spec Slice-2 coverage — every requirement maps to a task:**
- Migration 074 (`cc_cycle_payments`; `account_id` FK CASCADE; SmallInteger anchors; `amount Numeric(12,2) NOT NULL`; unique anchor; no `org_id`; no CHECK; `down_revision=073`) → Task 1 (+ MySQL up/down/up).
- `CcCyclePayment` model + `__init__` registration → Task 1.
- Validation (amount>0 → 422; past → 409; anchor resolves to a real CC cycle; gate on slug `credit_card` only) → Task 2.
- Endpoints (GET next-3 upcoming with dates from the resolver, normal org read; POST/PUT/DELETE {year}/{month}, owner/admin; org isolation via parent-account load) + registration → Task 3.
- Audit `account.cycle_payment.created/.updated/.deleted` post-commit → Task 3 + Task 5 (leave-CC bulk).
- Org-wipe/reset explicit subquery-scoped deletes before accounts + regression → Task 4.
- Leave-CC delete cascade + audit + regression → Task 5.
- Frontend `UpcomingCyclePayment` type + gated GET wiring + persist helper → Task 6; "Upcoming payments" mini-list (gated to per-cycle strategies; resolver-supplied windows; PUT on blur/Enter, DELETE on empty; exact copy; `·` not em-dash; token-clean primitives) → Task 7.

**Assumptions (all low-risk, re-verify at execution):**
1. "Today" is `date.today()` in the router; the service takes `today` as a parameter so unit tests pin it deterministically. Integration tests derive the current/future anchor from the GET collection (never hardcode absolute dates), except the far-past `2000/1` 409 case.
2. POST is strict-create (409 on existing); PUT is upsert (audit `.created` on insert, `.updated` with old+new on update). The FE uses PUT + DELETE only.
3. DELETE skips the current-or-future rule so a stale/past row can always be removed; only POST/PUT enforce the anchor rule.
4. `_seed_org` gains a `member` user (`Role.MEMBER`) + `member_id`; the org-data seed gains one `cc_cycle_payment` row so wipe/reset counts hold. Both `expected_keys` sets in `test_org_data_service.py` MUST gain `"cc_cycle_payments"` (exact set equality).
5. `_is_admin_user` / `_request_id` imported from `app.routers.accounts` (reuse per the spec); inline if cross-router private imports are disallowed.
6. The Task-1 router stub exists solely so the harness import resolves before Task 3.
7. Anchor→cycle mapping via day 1 of the anchor month is safe (close day always ≥ 1).
8. No forecast code (`account_balance_forecast_service` / `forecast_service`) is touched — Slice 3, out of scope.
