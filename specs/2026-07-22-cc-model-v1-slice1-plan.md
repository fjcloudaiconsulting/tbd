# Credit Card Model V1 — Slice 1 (schema + validation + display) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the four Credit Card metadata fields (`credit_limit`, `apr`, `payment_strategy`, `fixed_payment_amount`) to accounts end-to-end — migration, ORM, schema, CC-only validation service, router create/PUT wiring, leave-CC cascade clear, frontend form fields, and the quiet utilization/available-credit subline — WITHOUT the per-cycle payments store (Slice 2) or forecast synthesis (Slice 3).

**Architecture:** Fat-account-row idiom — the four columns live directly on `accounts` (nullable, NULL-at-rest on non-CC rows), mirroring the shipped `close_day` / `payment_day` / `payment_source_account_id` columns. A single-purpose `credit_card_service.validate_credit_card_fields` (mirroring `payment_source_service`) raises `HTTPException(422)`; the router calls it on create and inside the atomic PUT path against the resulting row state. The leave-CC cascade in `account_type_change_service` clears all four. Frontend gates form fields and a muted subline on `slug == 'credit_card'`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Alembic, MySQL 8 (SQLite in-memory for unit tests via aiosqlite); Next.js 16 + React 19 + TypeScript, Vitest + Testing Library.

## Global Constraints

- Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Pydantic v2 / MySQL 8 (native ENUM).
- Frontend: Next.js 16 + React 19 + TypeScript.
- No Off-Token colors (CI-blocked): the utilization subline uses `text-text-muted` only — NO status/accent color, even over-limit.
- No em-dashes anywhere: separators in copy use the middle dot `·` (U+00B7).
- No AI attribution in commit messages or PR bodies.
- Enums use `values_callable=lambda x: [e.value for e in x]` so values persist lowercase; native enum name `account_payment_strategy`.
- Migrations MUST be verified with `alembic upgrade head` against a real MySQL container — SQLite CI cannot catch native-ENUM DDL drift.
- Run backend tests in an ISOLATED compose project: `docker compose -p team-ccm1 up -d backend mysql redis` then `docker compose -p team-ccm1 exec backend pytest tests/...`. NEVER the default `pfv` stack. Every compose/exec command in the session carries `-p team-ccm1`. Frontend: `docker compose -p team-ccm1 exec frontend npm test -- tests/...`.
- New CC metadata fields on create/PUT emit NO new audit events (matches the `payment_source_account_id` precedent).
- Slice 1 EXCLUDES `cc_cycle_payments` (table/model/store/endpoint/UI), the org-wipe/reset deletes, and any forecast integration. Do NOT create `backend/app/models/cc_cycle_payment.py`, migration 074, or any cycle-payment code.

---

## File Structure

| File | Create / Modify | Responsibility |
|---|---|---|
| `backend/alembic/versions/073_credit_card_model_v1.py` | Create | Additive `ALTER TABLE accounts` — 3 `Numeric(12,2)` NULL columns + 1 native ENUM NULL column. `down_revision = "072_payment_source_account_id"`. Raw value tuple, no model import. |
| `backend/app/models/account.py` | Modify | Add `PaymentStrategy(str, enum.Enum)` + four `mapped_column`s on `Account` (NULL-at-rest, enum via `values_callable`). |
| `backend/app/services/credit_card_service.py` | Create | `validate_credit_card_fields(...)` — CC-only field validation, raises `HTTPException(422)`, returns `None`. |
| `backend/app/schemas/account.py` | Modify | Add the four fields to `AccountCreate`, `AccountUpdate`, `AccountResponse`. |
| `backend/app/routers/accounts.py` | Modify | `_to_response` exposes 4 fields; create path calls validate + inserts; PUT `touches_type_or_cc_columns` gains 4 keys; `_apply_non_type_fields` validates+applies the resulting row state via a new `resolved_slug` param. |
| `backend/app/services/account_type_change_service.py` | Modify | Leave-CC else-branch also clears the four new columns. |
| `frontend/lib/types.ts` | Modify | Add four fields to the `Account` interface. |
| `frontend/app/accounts/page.tsx` | Modify | CC-gated form fields (create + edit), body construction, `fixed_payment_amount` local-state clearing, utilization subline. |
| `backend/tests/test_account_credit_card_fields.py` | Create | Service unit tests + router create/PUT/leave-CC integration tests. |
| `frontend/tests/app/accounts-cc-model.test.tsx` | Create | Form gating, conditional fixed field, POST/PUT body, utilization subline copy. |

Test seeding, fixtures, and the FastAPI app-override harness are copied verbatim from `backend/tests/test_account_payment_source.py` and `frontend/tests/app/accounts-payment-source.test.tsx`.

> **Executor note:** line numbers below reflect the repo at plan-authoring time and may drift. Treat them as anchors, not guarantees — the TDD loop (failing test first) catches any drift. Re-locate by the quoted surrounding code if a line ref does not match.

---

## Task 1: Migration 073 + ORM columns/enum

**Files:**
- Create: `backend/alembic/versions/073_credit_card_model_v1.py`
- Modify: `backend/app/models/account.py` (add `import enum`; add `Enum as SAEnum` to the `sqlalchemy` import; insert `PaymentStrategy` class after `SYSTEM_ACCOUNT_TYPES`; add four columns to `Account` after `payment_source_account_id`).
- Test: `backend/tests/test_account_credit_card_fields.py` (new; enum-roundtrip test only in this task)

**Interfaces:**
- Produces: `PaymentStrategy(str, enum.Enum)` with members `FULL_BALANCE="full_balance"`, `MINIMUM_ONLY="minimum_only"`, `FIXED_AMOUNT="fixed_amount"`, `CUSTOM_PER_PERIOD="custom_per_period"`; `Account.credit_limit: Optional[Decimal]`, `Account.apr: Optional[Decimal]`, `Account.fixed_payment_amount: Optional[Decimal]`, `Account.payment_strategy: Optional[PaymentStrategy]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_account_credit_card_fields.py` with the fixture/harness block copied verbatim from `backend/tests/test_account_payment_source.py` (imports, `session_factory`, `_seed_org`, `worlds`, `_make_app`, `_account_row`), then append this first test. Add `from app.models.account import PaymentStrategy` below the existing model imports.

```python
def test_payment_strategy_enum_roundtrips_lowercase(session_factory, worlds):
    """The native-enum column stores and returns the lowercase value."""
    import asyncio

    a = worlds["a"]

    async def _write_and_read() -> object:
        async with session_factory() as db:
            row = (
                await db.execute(select(Account).where(Account.id == a["cc_id"]))
            ).scalar_one()
            row.payment_strategy = PaymentStrategy.FIXED_AMOUNT
            row.fixed_payment_amount = Decimal("75.00")
            row.credit_limit = Decimal("2000.00")
            row.apr = Decimal("19.99")
            await db.commit()
        async with session_factory() as db:
            return (
                await db.execute(select(Account).where(Account.id == a["cc_id"]))
            ).scalar_one()

    reread = asyncio.get_event_loop().run_until_complete(_write_and_read())
    assert reread.payment_strategy == PaymentStrategy.FIXED_AMOUNT
    assert reread.payment_strategy.value == "fixed_amount"
    assert reread.credit_limit == Decimal("2000.00")
    assert reread.apr == Decimal("19.99")
    assert reread.fixed_payment_amount == Decimal("75.00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py::test_payment_strategy_enum_roundtrips_lowercase -v`
Expected: FAIL with `AttributeError: 'Account' object has no attribute 'payment_strategy'` (and `ImportError` for `PaymentStrategy` if the import resolves first).

- [ ] **Step 3: Write minimal implementation (ORM)**

In `backend/app/models/account.py`, add `import enum` above the `from datetime import ...` line. In the `sqlalchemy` import block add `Enum as SAEnum,`. After the `SYSTEM_ACCOUNT_TYPES` list insert:

```python
class PaymentStrategy(str, enum.Enum):
    FULL_BALANCE = "full_balance"
    MINIMUM_ONLY = "minimum_only"
    FIXED_AMOUNT = "fixed_amount"
    CUSTOM_PER_PERIOD = "custom_per_period"
```

In `class Account`, immediately after the `payment_source_account_id` column insert:

```python
    # Credit Card Model V1 (specs/2026-07-22-cc-model-v1-design.md).
    # Four CC-only columns, NULL-at-rest on non-CC rows (fat-account-row
    # idiom, mirroring close_day). credit_limit is optional + non-enforcing;
    # apr is percent metadata [0,100]; fixed_payment_amount is required iff
    # payment_strategy == fixed_amount. payment_strategy is a native MySQL
    # ENUM; NULL means "resolver default (full_balance)". Validation lives in
    # credit_card_service, not the schema level.
    credit_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    apr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    fixed_payment_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    payment_strategy: Mapped[Optional[PaymentStrategy]] = mapped_column(
        SAEnum(
            PaymentStrategy,
            name="account_payment_strategy",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
```

- [ ] **Step 4: Write minimal implementation (migration)**

Create `backend/alembic/versions/073_credit_card_model_v1.py`:

```python
"""Add Credit Card Model V1 fields to accounts (Slice 1).

Revision ID: 073_credit_card_model_v1
Revises: 072_payment_source_account_id
Create Date: 2026-07-22

Four additive nullable columns on ``accounts`` (all NULL on non-CC rows,
mirroring the close_day fat-row invariant; no server_default):

    credit_limit          Numeric(12,2) NULL  -- optional, > 0 if set
    apr                   Numeric(12,2) NULL  -- percent metadata [0,100]
    fixed_payment_amount  Numeric(12,2) NULL  -- required iff strategy=fixed_amount
    payment_strategy      ENUM(...)     NULL  -- native MySQL enum, closed 4-set

``payment_strategy`` is a native MySQL ENUM. The set is genuinely CLOSED
(4 members), so the ABN .TAB enum-growth rule does not apply. Raw value
tuples are passed to ``sa.Enum`` (NOT the Python enum) so this migration
never imports app models, matching 045_reconciliation_state.py.

VERIFY with ``alembic upgrade head`` against a MySQL 8 container before
merge — SQLite CI cannot catch native-ENUM DDL drift.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "073_credit_card_model_v1"
down_revision: Union[str, None] = "072_payment_source_account_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lower-case values, matching the project's
# ``values_callable=lambda x: [e.value for e in x]`` convention.
_STRATEGIES = (
    "full_balance",
    "minimum_only",
    "fixed_amount",
    "custom_per_period",
)


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("credit_limit", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("apr", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("fixed_payment_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "payment_strategy",
            sa.Enum(*_STRATEGIES, name="account_payment_strategy"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Named enums on MySQL are stored inline on the column, so dropping
    # the column drops the enum. No separate Enum.drop() needed.
    op.drop_column("accounts", "payment_strategy")
    op.drop_column("accounts", "fixed_payment_amount")
    op.drop_column("accounts", "apr")
    op.drop_column("accounts", "credit_limit")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py::test_payment_strategy_enum_roundtrips_lowercase -v`
Expected: PASS

- [ ] **Step 6: Verify the migration on real MySQL**

Run: `docker compose -p team-ccm1 exec backend alembic upgrade head` then `docker compose -p team-ccm1 exec backend alembic downgrade -1` then `docker compose -p team-ccm1 exec backend alembic upgrade head`
Expected: three clean runs, no DDL error; head reports `073_credit_card_model_v1`.

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/073_credit_card_model_v1.py backend/app/models/account.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(accounts): add credit card model v1 columns and enum (migration 073)"
```

---

## Task 2: `credit_card_service.validate_credit_card_fields` + unit tests

**Files:**
- Create: `backend/app/services/credit_card_service.py`
- Test: `backend/tests/test_account_credit_card_fields.py` (append pure-function unit tests; these need no DB fixture)

**Interfaces:**
- Consumes: `PaymentStrategy` from `app.models.account` (Task 1).
- Produces: `validate_credit_card_fields(*, target_slug: Optional[str], credit_limit: Optional[Decimal], apr: Optional[Decimal], payment_strategy: Optional[PaymentStrategy | str], fixed_payment_amount: Optional[Decimal]) -> None` — raises `HTTPException(status_code=422, detail=...)`, returns `None` on success.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_account_credit_card_fields.py`. Add imports: `from app.services.credit_card_service import validate_credit_card_fields` and `from fastapi import HTTPException`.

```python
# ── credit_card_service.validate_credit_card_fields (pure unit) ────────────


def _expect_422(**kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        validate_credit_card_fields(**kwargs)
    assert exc.value.status_code == 422
    return exc.value


def test_non_cc_forbids_all_four_cc_fields():
    for field, value in [
        ("credit_limit", Decimal("100.00")),
        ("apr", Decimal("10.00")),
        ("payment_strategy", PaymentStrategy.FULL_BALANCE),
        ("fixed_payment_amount", Decimal("10.00")),
    ]:
        base = dict(
            target_slug="checking",
            credit_limit=None,
            apr=None,
            payment_strategy=None,
            fixed_payment_amount=None,
        )
        base[field] = value
        _expect_422(**base)


def test_cc_allows_all_null():
    # Bare CC account with nothing set is valid (limit optional, strategy NULL).
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


def test_cc_credit_limit_must_be_positive_when_set():
    _expect_422(
        target_slug="credit_card",
        credit_limit=Decimal("0"),
        apr=None,
        payment_strategy=None,
        fixed_payment_amount=None,
    )
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=Decimal("2500.00"),
        apr=None,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


@pytest.mark.parametrize("bad_apr", [Decimal("-1"), Decimal("100.01")])
def test_cc_apr_out_of_range_rejected(bad_apr):
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=bad_apr,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


@pytest.mark.parametrize("ok_apr", [Decimal("0"), Decimal("19.99"), Decimal("100")])
def test_cc_apr_in_range_ok(ok_apr):
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=None,
        apr=ok_apr,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


def test_fixed_amount_requires_positive_fixed_payment():
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT,
        fixed_payment_amount=None,
    )
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT,
        fixed_payment_amount=Decimal("0"),
    )
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT,
        fixed_payment_amount=Decimal("50.00"),
    )


@pytest.mark.parametrize(
    "strategy",
    [
        PaymentStrategy.FULL_BALANCE,
        PaymentStrategy.MINIMUM_ONLY,
        PaymentStrategy.CUSTOM_PER_PERIOD,
        None,
    ],
)
def test_fixed_payment_forbidden_for_non_fixed_strategy(strategy):
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=strategy,
        fixed_payment_amount=Decimal("50.00"),
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py -k "cc or fixed or apr or non_cc" -v`
Expected: FAIL with `ImportError: cannot import name 'validate_credit_card_fields' from 'app.services.credit_card_service'` (module does not exist).

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/credit_card_service.py`:

```python
"""Credit Card field validation service (Credit Card Model V1, Slice 1).

Single-purpose validator for the four CC-only columns on ``accounts``
(``specs/2026-07-22-cc-model-v1-design.md`` § Validation). Mirrors the
``payment_source_service`` convention: plain sync helper, raises
``HTTPException(422)`` on violation, returns ``None`` on success. Called
by the accounts router from both the create path and the shared
``_apply_non_type_fields`` update path against the resulting row state.

Rules:
  - Non-CC target: all four CC-only columns MUST be NULL.
  - CC target:
      * credit_limit optional; if set must be > 0 (non-enforcing: no
        balance <= limit check anywhere).
      * apr optional; if set must be in [0, 100] (stored as a percent).
      * fixed_payment_amount required and > 0 iff payment_strategy ==
        fixed_amount; forbidden otherwise.

Deliberate status divergence: these rules use 422 (matching
payment_source_service); the older close_day rules use 400. Accepted.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

from fastapi import HTTPException

from app.models.account import PaymentStrategy


_CC = "credit_card"
_APR_LO = Decimal("0")
_APR_HI = Decimal("100")


def validate_credit_card_fields(
    *,
    target_slug: Optional[str],
    credit_limit: Optional[Decimal],
    apr: Optional[Decimal],
    payment_strategy: Optional[Union[PaymentStrategy, str]],
    fixed_payment_amount: Optional[Decimal],
) -> None:
    """Validate the four CC-only field values against the target slug.

    Raises ``HTTPException(422)`` on any violation; returns ``None`` on
    success. ``payment_strategy`` may be a ``PaymentStrategy`` enum or its
    raw string value; both are normalized.
    """
    strategy = (
        payment_strategy.value
        if isinstance(payment_strategy, PaymentStrategy)
        else payment_strategy
    )

    if target_slug != _CC:
        if credit_limit is not None:
            raise HTTPException(
                status_code=422,
                detail="credit_limit is only allowed on credit_card accounts",
            )
        if apr is not None:
            raise HTTPException(
                status_code=422,
                detail="apr is only allowed on credit_card accounts",
            )
        if strategy is not None:
            raise HTTPException(
                status_code=422,
                detail="payment_strategy is only allowed on credit_card accounts",
            )
        if fixed_payment_amount is not None:
            raise HTTPException(
                status_code=422,
                detail="fixed_payment_amount is only allowed on credit_card accounts",
            )
        return

    # CC target.
    if credit_limit is not None and credit_limit <= 0:
        raise HTTPException(
            status_code=422,
            detail="credit_limit must be greater than 0",
        )
    if apr is not None and not (_APR_LO <= apr <= _APR_HI):
        raise HTTPException(
            status_code=422,
            detail="apr must be between 0 and 100",
        )

    if strategy == PaymentStrategy.FIXED_AMOUNT.value:
        if fixed_payment_amount is None or fixed_payment_amount <= 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "fixed_payment_amount is required and must be greater "
                    "than 0 for the fixed_amount payment strategy"
                ),
            )
    else:
        if fixed_payment_amount is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "fixed_payment_amount is only allowed with the "
                    "fixed_amount payment strategy"
                ),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py -k "cc or fixed or apr or non_cc" -v`
Expected: PASS (all parametrizations green).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/credit_card_service.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(accounts): add credit_card_service field validation"
```

---

## Task 3: Pydantic schema fields

**Files:**
- Modify: `backend/app/schemas/account.py` (import `PaymentStrategy`; add fields to `AccountCreate`, `AccountUpdate`, `AccountResponse`).
- Test: `backend/tests/test_account_credit_card_fields.py` (append read-compat test)

**Interfaces:**
- Consumes: `PaymentStrategy` from `app.models.account`.
- Produces: `AccountCreate.credit_limit / .apr / .payment_strategy / .fixed_payment_amount` (all optional, default `None`); same on `AccountUpdate`; `AccountResponse` exposes all four. `payment_strategy` typed `Optional[PaymentStrategy]`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_account_credit_card_fields.py`:

```python
# ── schema / read compatibility ────────────────────────────────────────────


def test_read_exposes_all_four_cc_fields(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        body = client.get(f"/api/v1/accounts/{a['cc_id']}").json()
    for key in ("credit_limit", "apr", "payment_strategy", "fixed_payment_amount"):
        assert key in body
        assert body[key] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py::test_read_exposes_all_four_cc_fields -v`
Expected: FAIL with `assert 'credit_limit' in body` (key absent from `AccountResponse`).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/schemas/account.py`, add after the pydantic import line:

```python
from app.models.account import PaymentStrategy
```

In `AccountCreate`, after `payment_source_account_id: Optional[int] = None` add:

```python
    # Credit Card Model V1 (Slice 1). CC-only; validated server-side in
    # credit_card_service. NULL on non-CC accounts. payment_strategy is a
    # native enum (NULL = resolver default). fixed_payment_amount is
    # required iff payment_strategy == fixed_amount.
    credit_limit: Optional[Decimal] = Field(
        default=None, max_digits=12, decimal_places=2
    )
    apr: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    payment_strategy: Optional[PaymentStrategy] = None
    fixed_payment_amount: Optional[Decimal] = Field(
        default=None, max_digits=12, decimal_places=2
    )
```

In `AccountUpdate`, after `opening_balance_date: Optional[date] = None` add the identical four field declarations.

In `AccountResponse`, after `payment_source_account_id: Optional[int] = None` add:

```python
    credit_limit: Optional[Decimal] = None
    apr: Optional[Decimal] = None
    payment_strategy: Optional[PaymentStrategy] = None
    fixed_payment_amount: Optional[Decimal] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py::test_read_exposes_all_four_cc_fields -v`
Expected: PASS (fields present and `null`; `_to_response` does not project them yet, so Pydantic emits their `None` defaults).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/account.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(accounts): add credit card fields to account schemas"
```

---

## Task 4: Router create-path wiring + `_to_response`

**Files:**
- Modify: `backend/app/routers/accounts.py` (import `validate_credit_card_fields`; `_to_response` add 4 fields; `create_account` validate call after `validate_create_payment_day` + insert kwargs).
- Test: `backend/tests/test_account_credit_card_fields.py` (append create-path tests)

**Interfaces:**
- Consumes: `validate_credit_card_fields` (Task 2), schema fields (Task 3), ORM columns (Task 1).
- Produces: create endpoint persists the four fields on CC accounts and returns them; rejects invalid combos with 422; `_to_response` projects all four.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_account_credit_card_fields.py`:

```python
# ── create path ────────────────────────────────────────────────────────────


def test_create_cc_with_fixed_amount_persists_fields(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Rewards Visa",
                "account_type_id": a["type_ids"]["credit_card"],
                "currency": "EUR",
                "close_day": 12,
                "credit_limit": "3000.00",
                "apr": "19.99",
                "payment_strategy": "fixed_amount",
                "fixed_payment_amount": "100.00",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["credit_limit"] == "3000.00"
    assert body["apr"] == "19.99"
    assert body["payment_strategy"] == "fixed_amount"
    assert body["fixed_payment_amount"] == "100.00"


def test_create_non_cc_with_credit_limit_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Bad Checking",
                "account_type_id": a["type_ids"]["checking"],
                "currency": "EUR",
                "credit_limit": "1000.00",
            },
        )
    assert res.status_code == 422, res.text


def test_create_cc_fixed_amount_without_fixed_payment_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Half Visa",
                "account_type_id": a["type_ids"]["credit_card"],
                "currency": "EUR",
                "close_day": 12,
                "payment_strategy": "fixed_amount",
            },
        )
    assert res.status_code == 422, res.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py -k "create" -v`
Expected: `test_create_cc_with_fixed_amount_persists_fields` FAILS (fields not persisted/projected); the two rejection tests FAIL (return 201 instead of 422).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/routers/accounts.py`, add to the imports (near the `payment_source_service` import):

```python
from app.services.credit_card_service import validate_credit_card_fields
```

In `_to_response`, before the closing `)`, add:

```python
        credit_limit=account.credit_limit,
        apr=account.apr,
        payment_strategy=account.payment_strategy,
        fixed_payment_amount=account.fixed_payment_amount,
```

In `create_account`, after the `validate_create_payment_day(...)` call and before the `await validate_payment_source_account(...)` call, insert:

```python
    # Credit Card Model V1 (Slice 1): the four CC-only fields. Non-CC
    # accounts must leave all four NULL; CC accounts get optional
    # credit_limit/apr and a strategy-gated fixed_payment_amount.
    validate_credit_card_fields(
        target_slug=target_type.slug,
        credit_limit=body.credit_limit,
        apr=body.apr,
        payment_strategy=body.payment_strategy,
        fixed_payment_amount=body.fixed_payment_amount,
    )
```

In the `kwargs = dict(...)` insert block, add before the closing `)`:

```python
        credit_limit=body.credit_limit,
        apr=body.apr,
        payment_strategy=body.payment_strategy,
        fixed_payment_amount=body.fixed_payment_amount,
```

> If the create-path type variable is not named `target_type`, use whichever local holds the resolved `AccountType` (whose `.slug` the close_day/payment_source validators already read).

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py -k "create or read_exposes" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/accounts.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(accounts): wire credit card fields into create path and response"
```

---

## Task 5: Router PUT wiring — `touches_type_or_cc_columns` + `_apply_non_type_fields`

**Files:**
- Modify: `backend/app/routers/accounts.py` — `touches_type_or_cc_columns`, the fast-path + atomic-path `_apply_non_type_fields` calls, and `_apply_non_type_fields` signature/body.
- Test: `backend/tests/test_account_credit_card_fields.py` (append PUT tests)

**Interfaces:**
- Consumes: `validate_credit_card_fields` (Task 2), `TypeChangeResult.new_type_slug` (existing).
- Produces: `_apply_non_type_fields(db, account, body, org_id, *, nested_default: bool, resolved_slug: Optional[str]) -> bool` — validates the resulting CC-field row state against `resolved_slug` and applies CC-field deltas via the `model_fields_set` idiom. PUT routes any CC-field edit through the atomic path.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_account_credit_card_fields.py`:

```python
# ── PUT path ────────────────────────────────────────────────────────────────


def test_put_sets_cc_fields_on_existing_cc(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={
                "credit_limit": "5000.00",
                "apr": "22.50",
                "payment_strategy": "minimum_only",
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credit_limit"] == "5000.00"
    assert body["apr"] == "22.50"
    assert body["payment_strategy"] == "minimum_only"
    assert body["fixed_payment_amount"] is None


def test_put_fixed_amount_requires_fixed_payment(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_strategy": "fixed_amount"},
        )
    assert res.status_code == 422, res.text


def test_put_credit_limit_on_non_cc_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['checking_id']}",
            json={"credit_limit": "1000.00"},
        )
    assert res.status_code == 422, res.text


def test_put_switch_to_fixed_amount_with_payment_ok(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={
                "payment_strategy": "fixed_amount",
                "fixed_payment_amount": "125.00",
            },
        )
    assert res.status_code == 200, res.text
    assert res.json()["fixed_payment_amount"] == "125.00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py -k "put" -v`
Expected: FAIL — a CC-field-only PUT takes the fast path and never validates/persists the CC fields.

- [ ] **Step 3: Write minimal implementation**

Extend `touches_type_or_cc_columns` to also test the four new `model_fields_set` keys:

```python
    touches_type_or_cc_columns = (
        body.account_type_id is not None
        or "close_day" in body.model_fields_set
        or "payment_day" in body.model_fields_set
        or "payment_day_relative_month" in body.model_fields_set
        or "credit_limit" in body.model_fields_set
        or "apr" in body.model_fields_set
        or "payment_strategy" in body.model_fields_set
        or "fixed_payment_amount" in body.model_fields_set
    )
```

Change `_apply_non_type_fields` to add a `resolved_slug` keyword (add `from typing import Optional` at the top of the file if not present):

```python
async def _apply_non_type_fields(
    db: AsyncSession,
    account: Account,
    body: AccountUpdate,
    org_id: int,
    *,
    nested_default: bool,
    resolved_slug: Optional[str],
) -> bool:
```

Inside `_apply_non_type_fields`, immediately after the `payment_source_account_id` block and before the opening_balance handling, insert:

```python
    # Credit Card Model V1 (Slice 1). Validate the RESULTING row state
    # (post-lock snapshot overlaid with payload deltas) against the
    # post-change slug, then apply the deltas. model_fields_set
    # distinguishes "omitted" (preserve) from an explicit value/null.
    cc_credit_limit = (
        body.credit_limit
        if "credit_limit" in body.model_fields_set
        else account.credit_limit
    )
    cc_apr = body.apr if "apr" in body.model_fields_set else account.apr
    cc_strategy = (
        body.payment_strategy
        if "payment_strategy" in body.model_fields_set
        else account.payment_strategy
    )
    cc_fixed = (
        body.fixed_payment_amount
        if "fixed_payment_amount" in body.model_fields_set
        else account.fixed_payment_amount
    )
    validate_credit_card_fields(
        target_slug=resolved_slug,
        credit_limit=cc_credit_limit,
        apr=cc_apr,
        payment_strategy=cc_strategy,
        fixed_payment_amount=cc_fixed,
    )
    if "credit_limit" in body.model_fields_set:
        account.credit_limit = body.credit_limit
    if "apr" in body.model_fields_set:
        account.apr = body.apr
    if "payment_strategy" in body.model_fields_set:
        account.payment_strategy = body.payment_strategy
    if "fixed_payment_amount" in body.model_fields_set:
        account.fixed_payment_amount = body.fixed_payment_amount
```

Update the fast-path call to pass the current slug:

```python
    opening_changed = await _apply_non_type_fields(
        db, account, body, actor_org_id, nested_default=True,
        resolved_slug=account.account_type.slug if account.account_type else None,
    )
```

Update the atomic-path call to pass the post-change slug from `type_result`:

```python
            opening_changed = await _apply_non_type_fields(
                svc_db, account, body, actor_org_id, nested_default=False,
                resolved_slug=type_result.new_type_slug,
            )
```

> If `TypeChangeResult` exposes the post-change slug under a different attribute, use that; the value must be the slug of the type the account will have AFTER the change.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py -k "put" -v`
Expected: PASS.

- [ ] **Step 5: Regression-check the existing account suites**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_payment_source.py tests/routers/test_accounts_change_type.py tests/test_account_opening_balance.py -q`
Expected: PASS (the new `resolved_slug` keyword must not break existing flows).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/accounts.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(accounts): validate and persist credit card fields on PUT"
```

---

## Task 6: Leave-CC cascade clear + regression test

**Files:**
- Modify: `backend/app/services/account_type_change_service.py` (else-branch of `apply_type_change_in_session`, after `account.payment_source_account_id = None`).
- Test: `backend/tests/test_account_credit_card_fields.py` (append cascade test)

**Interfaces:**
- Consumes: existing `apply_type_change_in_session` else-branch.
- Produces: leaving `credit_card` clears `credit_limit`, `apr`, `payment_strategy`, `fixed_payment_amount` to `None` server-side, regardless of payload.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_account_credit_card_fields.py`:

```python
# ── leave-CC cascade ────────────────────────────────────────────────────────


def test_leaving_cc_clears_all_four_cc_fields(session_factory, worlds):
    """Converting a CC to a non-CC type must null every CC-only column, so
    an asset row never silently retains a credit_limit/strategy no UI can
    surface (same bug class as the payment_source leave-CC cascade)."""
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        set_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={
                "credit_limit": "4000.00",
                "apr": "18.00",
                "payment_strategy": "fixed_amount",
                "fixed_payment_amount": "90.00",
            },
        )
        assert set_res.status_code == 200, set_res.text

        conv_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"account_type_id": a["type_ids"]["checking"]},
        )
        assert conv_res.status_code == 200, conv_res.text
        body = conv_res.json()
        assert body["credit_limit"] is None
        assert body["apr"] is None
        assert body["payment_strategy"] is None
        assert body["fixed_payment_amount"] is None

    import asyncio

    row = asyncio.get_event_loop().run_until_complete(
        _account_row(session_factory, a["cc_id"])
    )
    assert row.credit_limit is None
    assert row.apr is None
    assert row.payment_strategy is None
    assert row.fixed_payment_amount is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py::test_leaving_cc_clears_all_four_cc_fields -v`
Expected: FAIL — after conversion the CC fields still hold their set values.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/account_type_change_service.py`, in the `else:` branch of `apply_type_change_in_session` (after `account.payment_source_account_id = None`), add:

```python
        # Credit Card Model V1 (Slice 1): the four CC-only metadata columns
        # only make sense on a credit_card row. Clear them on leaving CC so
        # an asset account can't retain an orphaned credit_limit / apr /
        # payment_strategy / fixed_payment_amount (mirrors the close_day and
        # payment_source leave-CC cascades above).
        account.credit_limit = None
        account.apr = None
        account.payment_strategy = None
        account.fixed_payment_amount = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py::test_leaving_cc_clears_all_four_cc_fields -v`
Expected: PASS.

- [ ] **Step 5: Run the full new suite + change-type regression**

Run: `docker compose -p team-ccm1 exec backend pytest tests/test_account_credit_card_fields.py tests/routers/test_accounts_change_type.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/account_type_change_service.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(accounts): clear credit card fields when leaving credit_card type"
```

---

## Task 7: Frontend `Account` type + CC-gated form fields

**Files:**
- Modify: `frontend/lib/types.ts` (`Account` interface).
- Modify: `frontend/app/accounts/page.tsx` — create + edit state hooks, `startEditAcct`, `handleAddAccount` body + reset, `_doSaveAcct` body, create form JSX, edit form JSX, strategy-select `onChange` clearing.
- Test: `frontend/tests/app/accounts-cc-model.test.tsx` (new — form gating + body construction)

**Interfaces:**
- Consumes: the `AccountResponse` JSON shape (Tasks 3-6).
- Produces: `Account.credit_limit / .apr / .payment_strategy / .fixed_payment_amount`; create POST body and edit PUT body carry the four fields for CC accounts only.

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/app/accounts-cc-model.test.tsx`. Copy the harness (imports, `vi.mock` blocks, `USER`, `ACCOUNT_TYPES`, `CHECKING`/`SAVINGS`/`CC` fixtures, `mockApi`, `setupAuth`, `beforeEach`, `openEditRow`) verbatim from `frontend/tests/app/accounts-payment-source.test.tsx`, then extend the `CC` fixture with `credit_limit: "2000.00", apr: "19.99", payment_strategy: "minimum_only", fixed_payment_amount: null` and append:

```tsx
describe("CC Model — form fields", () => {
  test("edit row shows credit limit, APR and strategy for a CC account", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    expect(await screen.findByLabelText(/Credit limit/i)).toBeTruthy();
    expect(screen.getByLabelText(/APR/i)).toBeTruthy();
    expect(screen.getByLabelText(/Payment strategy/i)).toBeTruthy();
  });

  test("fixed payment amount appears only under the fixed_amount strategy", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    expect(screen.queryByLabelText(/Fixed payment amount/i)).toBeNull();
    fireEvent.change(await screen.findByLabelText(/Payment strategy/i), {
      target: { value: "fixed_amount" },
    });
    expect(await screen.findByLabelText(/Fixed payment amount/i)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Payment strategy/i), {
      target: { value: "full_balance" },
    });
    expect(screen.queryByLabelText(/Fixed payment amount/i)).toBeNull();
  });

  test("PUT body carries the four CC fields for a CC account", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText(/Credit limit/i), {
      target: { value: "5000" },
    });
    fireEvent.change(screen.getByLabelText(/APR/i), { target: { value: "21.5" } });
    fireEvent.change(screen.getByLabelText(/Payment strategy/i), {
      target: { value: "fixed_amount" },
    });
    fireEvent.change(await screen.findByLabelText(/Fixed payment amount/i), {
      target: { value: "150" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const putCall = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts/11" && init?.method === "PUT",
        );
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String(putCall![1]?.body));
      expect(body.credit_limit).toBe("5000");
      expect(body.apr).toBe("21.5");
      expect(body.payment_strategy).toBe("fixed_amount");
      expect(body.fixed_payment_amount).toBe("150");
    });
  });

  test("non-CC edit row shows none of the CC fields", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(10);
    await screen.findByLabelText("Account type");
    expect(screen.queryByLabelText(/Credit limit/i)).toBeNull();
    expect(screen.queryByLabelText(/Payment strategy/i)).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec frontend npm test -- tests/app/accounts-cc-model.test.tsx`
Expected: FAIL — `Unable to find a label "Credit limit"` etc.

- [ ] **Step 3: Implement the `Account` type**

In `frontend/lib/types.ts`, inside `interface Account` (before the closing `}`) add:

```typescript
  // Credit Card Model V1 (Slice 1). CC-only; null on asset accounts. The
  // API serializes Decimals as strings. payment_strategy is a closed enum;
  // null means "resolver default (full_balance)".
  credit_limit?: number | string | null;
  apr?: number | string | null;
  payment_strategy?:
    | "full_balance"
    | "minimum_only"
    | "fixed_amount"
    | "custom_per_period"
    | null;
  fixed_payment_amount?: number | string | null;
```

- [ ] **Step 4: Implement the create-form state + fields + POST body**

Add create-form state after `acctPaymentSource`:

```typescript
  // Credit Card Model V1 (Slice 1) — CC-only create fields. "" = unset.
  const [acctCreditLimit, setAcctCreditLimit] = useState("");
  const [acctApr, setAcctApr] = useState("");
  const [acctPaymentStrategy, setAcctPaymentStrategy] = useState("");
  const [acctFixedPayment, setAcctFixedPayment] = useState("");
```

In `handleAddAccount`, replace the `...(isCC ? { payment_source_account_id: ... } : {})` spread with:

```typescript
          ...(isCC
            ? {
                payment_source_account_id:
                  acctPaymentSource === "" ? null : acctPaymentSource,
                credit_limit: acctCreditLimit === "" ? null : acctCreditLimit,
                apr: acctApr === "" ? null : acctApr,
                payment_strategy:
                  acctPaymentStrategy === "" ? null : acctPaymentStrategy,
                fixed_payment_amount:
                  acctPaymentStrategy === "fixed_amount" && acctFixedPayment !== ""
                    ? acctFixedPayment
                    : null,
              }
            : {}),
```

In the post-create reset block, add:

```typescript
      setAcctCreditLimit(""); setAcctApr(""); setAcctPaymentStrategy(""); setAcctFixedPayment("");
```

In the create-form JSX: after the close-day block and before the Paid-from block, insert the Credit limit + APR fields; after the Paid-from block insert the Payment strategy + conditional Fixed payment fields:

```tsx
                  {selectedType?.slug === "credit_card" && (
                    <div>
                      <label htmlFor="acct-credit-limit" className={label}>Credit limit</label>
                      <input id="acct-credit-limit" type="number" step="0.01" min={0} value={acctCreditLimit} onChange={(e) => setAcctCreditLimit(e.target.value)} className={`w-40 ${input}`} placeholder="2000.00" />
                    </div>
                  )}
                  {selectedType?.slug === "credit_card" && (
                    <div>
                      <label htmlFor="acct-apr" className={label}>APR (%)</label>
                      <input id="acct-apr" type="number" step="0.01" min={0} max={100} value={acctApr} onChange={(e) => setAcctApr(e.target.value)} className={`w-28 ${input}`} placeholder="19.99" />
                    </div>
                  )}
```

and after the Paid-from block:

```tsx
                  {selectedType?.slug === "credit_card" && (
                    <div>
                      <label htmlFor="acct-payment-strategy" className={label}>Payment strategy</label>
                      <select
                        id="acct-payment-strategy"
                        value={acctPaymentStrategy}
                        onChange={(e) => {
                          setAcctPaymentStrategy(e.target.value);
                          if (e.target.value !== "fixed_amount") setAcctFixedPayment("");
                        }}
                        className={input}
                      >
                        <option value="">(default: pay full balance)</option>
                        <option value="full_balance">Pay full balance</option>
                        <option value="minimum_only">Minimum only</option>
                        <option value="fixed_amount">Fixed amount</option>
                        <option value="custom_per_period">Custom per period</option>
                      </select>
                    </div>
                  )}
                  {selectedType?.slug === "credit_card" && acctPaymentStrategy === "fixed_amount" && (
                    <div>
                      <label htmlFor="acct-fixed-payment" className={label}>Fixed payment amount</label>
                      <input id="acct-fixed-payment" type="number" step="0.01" min={0} value={acctFixedPayment} onChange={(e) => setAcctFixedPayment(e.target.value)} className={`w-40 ${input}`} placeholder="100.00" />
                    </div>
                  )}
```

- [ ] **Step 5: Implement the edit-form state + fields + PUT body**

Add edit state after `editAcctPaymentSource`:

```typescript
  // Credit Card Model V1 (Slice 1) — CC-only edit fields. "" = unset.
  const [editAcctCreditLimit, setEditAcctCreditLimit] = useState("");
  const [editAcctApr, setEditAcctApr] = useState("");
  const [editAcctPaymentStrategy, setEditAcctPaymentStrategy] = useState("");
  const [editAcctFixedPayment, setEditAcctFixedPayment] = useState("");
```

In `startEditAcct`, seed them:

```typescript
    setEditAcctCreditLimit(a.credit_limit != null ? String(a.credit_limit) : "");
    setEditAcctApr(a.apr != null ? String(a.apr) : "");
    setEditAcctPaymentStrategy(a.payment_strategy ?? "");
    setEditAcctFixedPayment(a.fixed_payment_amount != null ? String(a.fixed_payment_amount) : "");
```

In `_doSaveAcct`, inside the `if (isCC) { ... }` block (after `body.payment_source_account_id = ...`), add:

```typescript
      body.credit_limit = editAcctCreditLimit === "" ? null : editAcctCreditLimit;
      body.apr = editAcctApr === "" ? null : editAcctApr;
      body.payment_strategy =
        editAcctPaymentStrategy === "" ? null : editAcctPaymentStrategy;
      body.fixed_payment_amount =
        editAcctPaymentStrategy === "fixed_amount" && editAcctFixedPayment !== ""
          ? editAcctFixedPayment
          : null;
```

In the edit-row type-select `onChange` non-CC branch (`if (nextSlug !== "credit_card")`), add local-state clearing:

```typescript
                            setEditAcctCreditLimit("");
                            setEditAcctApr("");
                            setEditAcctPaymentStrategy("");
                            setEditAcctFixedPayment("");
```

In the edit-form JSX, after the Paid-from block, add the CC fields as their own gated blocks:

```tsx
                    {editingTypeSlug === "credit_card" && (
                      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:gap-3">
                        <div className="w-full sm:w-40">
                          <label htmlFor={`edit-acct-credit-limit-${a.id}`} className={label}>Credit limit</label>
                          <input id={`edit-acct-credit-limit-${a.id}`} type="number" step="0.01" min={0} value={editAcctCreditLimit} onChange={(e) => setEditAcctCreditLimit(e.target.value)} className={`w-full text-sm ${input}`} />
                        </div>
                        <div className="w-full sm:w-28">
                          <label htmlFor={`edit-acct-apr-${a.id}`} className={label}>APR (%)</label>
                          <input id={`edit-acct-apr-${a.id}`} type="number" step="0.01" min={0} max={100} value={editAcctApr} onChange={(e) => setEditAcctApr(e.target.value)} className={`w-full text-sm ${input}`} />
                        </div>
                      </div>
                    )}
                    {editingTypeSlug === "credit_card" && (
                      <div className="w-full sm:w-72">
                        <label htmlFor={`edit-acct-payment-strategy-${a.id}`} className={label}>Payment strategy</label>
                        <select
                          id={`edit-acct-payment-strategy-${a.id}`}
                          value={editAcctPaymentStrategy}
                          onChange={(e) => {
                            setEditAcctPaymentStrategy(e.target.value);
                            if (e.target.value !== "fixed_amount") setEditAcctFixedPayment("");
                          }}
                          className={`w-full text-sm ${input}`}
                        >
                          <option value="">(default: pay full balance)</option>
                          <option value="full_balance">Pay full balance</option>
                          <option value="minimum_only">Minimum only</option>
                          <option value="fixed_amount">Fixed amount</option>
                          <option value="custom_per_period">Custom per period</option>
                        </select>
                      </div>
                    )}
                    {editingTypeSlug === "credit_card" && editAcctPaymentStrategy === "fixed_amount" && (
                      <div className="w-full sm:w-40">
                        <label htmlFor={`edit-acct-fixed-payment-${a.id}`} className={label}>Fixed payment amount</label>
                        <input id={`edit-acct-fixed-payment-${a.id}`} type="number" step="0.01" min={0} value={editAcctFixedPayment} onChange={(e) => setEditAcctFixedPayment(e.target.value)} className={`w-full text-sm ${input}`} />
                      </div>
                    )}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec frontend npm test -- tests/app/accounts-cc-model.test.tsx`
Expected: PASS (form-fields describe block).

- [ ] **Step 7: Type-check**

Run: `docker compose -p team-ccm1 exec frontend npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add frontend/lib/types.ts frontend/app/accounts/page.tsx frontend/tests/app/accounts-cc-model.test.tsx
git commit -m "feat(accounts): add credit card model fields to the accounts form"
```

---

## Task 8: Frontend utilization / available-credit subline

**Files:**
- Modify: `frontend/app/accounts/page.tsx` — balance-column block, after the Opening hint.
- Test: `frontend/tests/app/accounts-cc-model.test.tsx` (append subline describe block)

**Interfaces:**
- Consumes: `Account.credit_limit`, `Account.balance`, `Account.currency`, `formatAmount` (existing import).
- Produces: a muted subline rendered only when `account_type_slug === "credit_card" && Number(credit_limit) > 0`.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/tests/app/accounts-cc-model.test.tsx`:

```tsx
describe("CC Model — utilization subline", () => {
  function ccWith(balance: string, credit_limit: string | null) {
    return { ...CC, balance, credit_limit };
  }

  test("within-limit shows 'Using n% of limit · <available> <ccy> left'", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("-500.00", "2000.00")]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/Using 25% of limit · 1,500\.00 EUR left/)).toBeTruthy();
  });

  test("zero outstanding shows the full-limit copy", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("0.00", "2000.00")]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/0% used · full limit available/)).toBeTruthy();
  });

  test("over-limit shows the '<over> <ccy> over' copy (uncapped %)", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("-2500.00", "2000.00")]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/Using 125% of limit · 500\.00 EUR over/)).toBeTruthy();
  });

  test("no subline when credit_limit is null or zero", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("-500.00", null)]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).queryByText(/of limit/)).toBeNull();
    expect(within(row).queryByText(/full limit available/)).toBeNull();
  });
});
```

> If the row does not already expose a `data-testid="account-row-<id>"`, add one to the row container in Task 7's edits (or select via an existing stable text/role the harness already uses). Do NOT change unrelated row markup beyond the testid.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec frontend npm test -- tests/app/accounts-cc-model.test.tsx -t "utilization"`
Expected: FAIL — copy strings not found (subline not rendered).

- [ ] **Step 3: Write minimal implementation**

In `frontend/app/accounts/page.tsx`, inside the balance-column `<div>` (after the Opening-balance hint block), insert:

```tsx
                      {/* Credit Card Model V1 (Slice 1) — utilization /
                          available-credit subline. Render only for a CC
                          with a positive credit_limit; otherwise stay
                          silent (no "—"). Liabilities are negative
                          balances: outstanding = max(0, -bal). No color
                          band, even over-limit (owner-permitted state; the
                          balance sign already carries the "you owe"
                          signal). Separator is a middle dot, no em-dash. */}
                      {a.account_type_slug === "credit_card" && Number(a.credit_limit) > 0
                        ? (() => {
                            const limit = Number(a.credit_limit);
                            const bal = Number(a.balance);
                            const outstanding = Math.max(0, -bal);
                            const util = Math.round((outstanding / limit) * 100);
                            const available = limit + bal;
                            const over = outstanding - limit;
                            let text: string;
                            if (outstanding === 0) {
                              text = "0% used · full limit available";
                            } else if (over > 0) {
                              text = `Using ${util}% of limit · ${formatAmount(over)} ${a.currency} over`;
                            } else {
                              text = `Using ${util}% of limit · ${formatAmount(available)} ${a.currency} left`;
                            }
                            return (
                              <span className="text-xs tabular-nums text-text-muted">
                                {text}
                              </span>
                            );
                          })()
                        : null}
```

> `formatAmount` must render `1500` as `1,500.00` (grouped, 2 decimals) for the test copy to match. If the existing helper differs, use whatever the row already uses for the Opening/Pending amounts so the subline matches the page's number format, and adjust the test's expected strings to that format.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec frontend npm test -- tests/app/accounts-cc-model.test.tsx`
Expected: PASS (all describe blocks, including Task 7's).

- [ ] **Step 5: Type-check + full accounts-page regression**

Run: `docker compose -p team-ccm1 exec frontend npx tsc --noEmit` then `docker compose -p team-ccm1 exec frontend npm test -- tests/app/accounts-payment-source.test.tsx tests/app/accounts-edit-type.test.tsx tests/app/accounts-pending-visibility.test.tsx`
Expected: no type errors; existing accounts-page suites PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/accounts/page.tsx frontend/tests/app/accounts-cc-model.test.tsx
git commit -m "feat(accounts): add credit utilization subline to the accounts list"
```

---

## Self-review notes

**Spec Slice-1 coverage — every requirement maps to a task:**
- Migration 073 (3 Numeric + native ENUM, `down_revision=072`, raw tuple, `045` idiom) → Task 1 (+ MySQL verification step 6).
- ORM `PaymentStrategy` + 4 columns (`values_callable` lowercase, `name="account_payment_strategy"`, NULL-at-rest) → Task 1.
- Pydantic `AccountCreate`/`AccountUpdate`/`AccountResponse` fields → Task 3.
- `credit_card_service.validate_credit_card_fields` (non-CC forbids all four; CC optional `credit_limit>0`; `apr in [0,100]`; `fixed_payment_amount` required+>0 iff `fixed_amount` else forbidden; no `require_credit_limit`; no balance<=limit check; 422) → Task 2.
- Router `_to_response` + create validate/insert → Task 4; PUT `touches_type_or_cc_columns` + validate resulting state in the atomic path → Task 5.
- Leave-CC cascade clears all four → Task 6.
- Frontend `Account` type + create/edit form fields + `fixed_payment_amount` clearing + create/PUT bodies (CC-only) → Task 7.
- Utilization/available-credit subline (gated on `slug==credit_card && credit_limit>0`, uncapped %, exact copy with `·`, no color) → Task 8.

**Assumptions (all low-risk, re-verify at execution):**
1. The spec says "validate ... inside `_apply_non_type_fields`"; a new `resolved_slug` keyword lets one shared function validate against the post-change slug (fast path = current slug; atomic path = `type_result.new_type_slug`).
2. `AccountUpdate.payment_strategy` is typed `Optional[PaymentStrategy]` (schema imports the ORM enum). If the team forbids schema→model imports, substitute `Literal["full_balance","minimum_only","fixed_amount","custom_per_period"]` — no other code changes.
3. Money values cross the API as JSON strings (Pydantic `Decimal`), so frontend tests assert string bodies and the subline coerces with `Number(...)`.
4. Compose project name `team-ccm1` is illustrative; use any unique `team-<name>` consistently across the session per CLAUDE.md.
5. No `cc_cycle_payments` model/table/store/endpoint, no org-wipe/reset deletes, and no forecast code are created — those are Slice 2 and Slice 3 and are explicitly out of scope here.
