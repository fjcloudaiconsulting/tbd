"""LAI.3 — Smart Budget Rebalance service tests.

Covers the public ``suggest_rebalance`` entry point in
``budget_rebalance_service``. The dispatch substrate
(``call_llm_structured``) is patched at the module boundary so these
tests are completely offline.

Cases:
- No current-period budgets → ``empty_no_budgets``.
- Budgets exist but no settled history → ``empty_no_history``.
- Happy path: LLM returns valid suggestions → ``ok`` + shaped deltas.
- LLM returns an unknown ``category_id`` → ``llm_unavailable`` (the
  cross-org / hallucination guard).
- ``NoRoutingConfigured`` from the dispatch layer → ``llm_unavailable``.
- ``StructuredOutputError`` (retry budget exhausted) → ``llm_unavailable``.
- Inputs to the prompt are aggregates only — no raw transaction data
  leaks into the message payload.
"""
from __future__ import annotations

import base64
import datetime
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.models.user import Organization, Role, User
from app.services import budget_rebalance_service
from app.services.ai_dispatch import (
    NoRoutingConfigured,
    StructuredDispatchResult,
)
from app.services.ai_providers.base import (
    StructuredOutputError,
    StructuredResponse,
)


# ---------- fixtures --------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> Organization:
    o = Organization(name="Acme", billing_cycle_day=1)
    db.add(o)
    await db.commit()
    return o


@pytest_asyncio.fixture
async def user(db: AsyncSession, org: Organization) -> User:
    u = User(
        org_id=org.id,
        username="owner",
        email="owner@example.com",
        password_hash="x" * 64,
        role=Role.OWNER,
    )
    db.add(u)
    await db.commit()
    return u


@pytest_asyncio.fixture
async def period(db: AsyncSession, org: Organization) -> BillingPeriod:
    today = datetime.date.today()
    p = BillingPeriod(org_id=org.id, start_date=today.replace(day=1))
    db.add(p)
    await db.commit()
    return p


@pytest_asyncio.fixture
async def categories(
    db: AsyncSession, org: Organization
) -> dict[str, Category]:
    groceries = Category(
        org_id=org.id,
        name="Groceries",
        type=CategoryType.EXPENSE,
        parent_id=None,
    )
    dining = Category(
        org_id=org.id,
        name="Dining",
        type=CategoryType.EXPENSE,
        parent_id=None,
    )
    db.add_all([groceries, dining])
    await db.commit()
    return {"groceries": groceries, "dining": dining}


@pytest_asyncio.fixture
async def budgets(
    db: AsyncSession,
    org: Organization,
    period: BillingPeriod,
    categories: dict[str, Category],
) -> dict[str, Budget]:
    g = Budget(
        org_id=org.id,
        category_id=categories["groceries"].id,
        amount=Decimal("400.00"),
        period_start=period.start_date,
        period_end=period.end_date,
    )
    d = Budget(
        org_id=org.id,
        category_id=categories["dining"].id,
        amount=Decimal("200.00"),
        period_start=period.start_date,
        period_end=period.end_date,
    )
    db.add_all([g, d])
    await db.commit()
    return {"groceries": g, "dining": d}


async def _seed_account(db: AsyncSession, org: Organization, user: User):
    """Minimal account row so we can insert transactions."""
    from app.models.account import Account, AccountType

    at = AccountType(org_id=org.id, name="Checking", slug="checking")
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id,
        account_type_id=at.id,
        name="checking",
        balance=Decimal("1000.00"),
        currency="USD",
    )
    db.add(acct)
    await db.commit()
    return acct


async def _seed_history(
    db: AsyncSession,
    *,
    org: Organization,
    user: User,
    category: Category,
    amount: Decimal,
    settled: datetime.date,
):
    acct = await _seed_account(db, org, user)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=category.id,
        description="x",
        amount=amount,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        settled_date=settled,
        date=settled,
    )
    db.add(tx)
    await db.commit()


# ---------- empty paths ----------------------------------------------


@pytest.mark.asyncio
async def test_no_budgets_returns_empty_no_budgets(
    db: AsyncSession, org: Organization, user: User, period: BillingPeriod
):
    out = await budget_rebalance_service.suggest_rebalance(db, org_id=org.id)
    assert out.status == "empty_no_budgets"
    assert out.suggestions == []


@pytest.mark.asyncio
async def test_budgets_but_no_history_returns_empty_no_history(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
    budgets,
):
    out = await budget_rebalance_service.suggest_rebalance(db, org_id=org.id)
    assert out.status == "empty_no_history"
    assert out.suggestions == []


# ---------- happy path ------------------------------------------------


def _make_structured_result(parsed: dict) -> StructuredDispatchResult:
    return StructuredDispatchResult(
        response=StructuredResponse(
            parsed=parsed,
            raw_text="{}",
            prompt_tokens=10,
            completion_tokens=20,
            model="gpt-4o-mini",
            retries_used=0,
        ),
        ledger_id=1,
    )


@pytest.mark.asyncio
async def test_happy_path_returns_shaped_suggestions(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
    categories,
    budgets,
):
    """LLM returns valid suggestions for known category_ids → ok."""
    today = datetime.date.today()
    last_month = (today.replace(day=1) - datetime.timedelta(days=1))
    await _seed_history(
        db,
        org=org,
        user=user,
        category=categories["groceries"],
        amount=Decimal("450.00"),
        settled=last_month,
    )

    captured_messages: list[list[dict]] = []

    async def fake_call(
        *args, messages, response_schema, feature_key, org_id, **kw
    ):
        captured_messages.append(messages)
        return _make_structured_result(
            {
                "summary": "Move money to groceries; you consistently overspend.",
                "suggestions": [
                    {
                        "category_id": categories["groceries"].id,
                        "suggested_amount": 450.0,
                        "reasoning": "Trailing 3-month spend is well above the current limit.",
                    },
                    {
                        "category_id": categories["dining"].id,
                        "suggested_amount": 150.0,
                        "reasoning": "You've been under budget here for months.",
                    },
                ],
            }
        )

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        out = await budget_rebalance_service.suggest_rebalance(
            db, org_id=org.id
        )

    assert out.status == "ok"
    assert out.summary.startswith("Move money to groceries")
    assert len(out.suggestions) == 2
    g_sugg = next(
        s for s in out.suggestions
        if s.category_id == categories["groceries"].id
    )
    assert g_sugg.suggested_amount == Decimal("450.00")
    assert g_sugg.current_amount == Decimal("400.00")
    assert g_sugg.delta_amount == Decimal("50.00")
    d_sugg = next(
        s for s in out.suggestions
        if s.category_id == categories["dining"].id
    )
    assert d_sugg.delta_amount == Decimal("-50.00")

    # Aggregates-only: prompt must NOT carry raw transaction text. The
    # _seed_history call above set description="x"; that string must
    # not appear anywhere in the message payload.
    flat = " ".join(
        m.get("content", "") for ms in captured_messages for m in ms
    )
    assert "description" not in flat.lower()
    # The trailing-spend aggregate (last_3mo_avg) for groceries is
    # $450 / 3 = $150. The aggregate MUST be in the prompt.
    assert "150.0" in flat or "150" in flat


# ---------- defense-in-depth: unknown category_id --------------------


@pytest.mark.asyncio
async def test_unknown_category_id_in_response_returns_llm_unavailable(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
    categories,
    budgets,
):
    """LLM returns a category_id that is NOT in the org's budget set.

    The service refuses to surface partial/hallucinated data and
    returns the friendly empty state.
    """
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)
    await _seed_history(
        db,
        org=org,
        user=user,
        category=categories["groceries"],
        amount=Decimal("100.00"),
        settled=last_month,
    )

    async def fake_call(*args, **kw):
        return _make_structured_result(
            {
                "summary": "drift",
                "suggestions": [
                    {
                        # 99999 is NOT a real category for this org.
                        "category_id": 99999,
                        "suggested_amount": 250.0,
                        "reasoning": "I am hallucinating.",
                    }
                ],
            }
        )

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        out = await budget_rebalance_service.suggest_rebalance(
            db, org_id=org.id
        )

    assert out.status == "llm_unavailable"
    assert out.suggestions == []


# ---------- LLM unavailable paths ------------------------------------


@pytest.mark.asyncio
async def test_no_routing_returns_llm_unavailable(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
    categories,
    budgets,
):
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)
    await _seed_history(
        db,
        org=org,
        user=user,
        category=categories["groceries"],
        amount=Decimal("100.00"),
        settled=last_month,
    )

    async def fake_call(*args, **kw):
        raise NoRoutingConfigured()

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        out = await budget_rebalance_service.suggest_rebalance(
            db, org_id=org.id
        )

    assert out.status == "llm_unavailable"
    assert "AI rebalance is temporarily unavailable" in out.summary


@pytest.mark.asyncio
async def test_structured_output_exhausted_returns_llm_unavailable(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
    categories,
    budgets,
):
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)
    await _seed_history(
        db,
        org=org,
        user=user,
        category=categories["groceries"],
        amount=Decimal("100.00"),
        settled=last_month,
    )

    async def fake_call(*args, **kw):
        raise StructuredOutputError("STATUS_ERROR_STRUCTURED_OUTPUT")

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        out = await budget_rebalance_service.suggest_rebalance(
            db, org_id=org.id
        )

    assert out.status == "llm_unavailable"


# ---------- contract: no auto-apply -----------------------------------


@pytest.mark.asyncio
async def test_service_never_mutates_budgets(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
    categories,
    budgets,
):
    """The service is read-only: even on the ``ok`` path, the budget
    rows must not be mutated. Application is the frontend's job, via
    the existing budget update endpoints.
    """
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)
    await _seed_history(
        db,
        org=org,
        user=user,
        category=categories["groceries"],
        amount=Decimal("100.00"),
        settled=last_month,
    )

    before_groceries = budgets["groceries"].amount
    before_dining = budgets["dining"].amount

    async def fake_call(*args, **kw):
        return _make_structured_result(
            {
                "summary": "shift",
                "suggestions": [
                    {
                        "category_id": categories["groceries"].id,
                        "suggested_amount": 999.0,
                        "reasoning": "test",
                    }
                ],
            }
        )

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        await budget_rebalance_service.suggest_rebalance(db, org_id=org.id)

    await db.refresh(budgets["groceries"])
    await db.refresh(budgets["dining"])
    assert budgets["groceries"].amount == before_groceries
    assert budgets["dining"].amount == before_dining


# ---------- parent/child rollup correctness --------------------------


@pytest.mark.asyncio
async def test_child_with_own_budget_is_not_double_counted_via_parent(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
):
    """When a parent category AND its child BOTH have budgets, the
    child's transactions must NOT roll up into the parent's facts row
    (otherwise the same dollars are counted in both rows).

    Seeds: Housing (parent, $2000 budget), Rent (child of Housing,
    $1500 budget). $1000 of expenses settled on Rent last month.

    Expected: in the prompt, Rent's avg appears in Rent's facts row
    (~333/mo); Housing's facts row sees $0 (Housing itself has no
    transactions and Rent's are absorbed into Rent's own row).
    """
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)

    housing = Category(
        org_id=org.id, name="Housing", type=CategoryType.EXPENSE, parent_id=None,
    )
    db.add(housing)
    await db.commit()
    rent = Category(
        org_id=org.id, name="Rent", type=CategoryType.EXPENSE, parent_id=housing.id,
    )
    db.add(rent)
    await db.commit()

    db.add_all([
        Budget(
            org_id=org.id, category_id=housing.id,
            amount=Decimal("2000.00"),
            period_start=period.start_date, period_end=period.end_date,
        ),
        Budget(
            org_id=org.id, category_id=rent.id,
            amount=Decimal("1500.00"),
            period_start=period.start_date, period_end=period.end_date,
        ),
    ])
    await db.commit()

    await _seed_history(
        db, org=org, user=user, category=rent,
        amount=Decimal("1000.00"), settled=last_month,
    )

    captured_payloads: list[dict] = []

    async def fake_call(*args, messages, **kw):
        # The user-content message has the aggregates dict embedded as
        # its content. Extract by finding the categories list in the
        # JSON-ish payload.
        for m in messages:
            content = m.get("content", "")
            if "categories" in content and "category_id" in content:
                # Eval the payload — _build_messages writes it via repr(),
                # so it's a Python literal, NOT JSON.
                import ast, re
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match:
                    captured_payloads.append(ast.literal_eval(match.group()))
        return _make_structured_result(
            {"summary": "ok", "suggestions": []}
        )

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        await budget_rebalance_service.suggest_rebalance(db, org_id=org.id)

    assert captured_payloads, "expected the prompt to carry an aggregates payload"
    cats = captured_payloads[0]["categories"]
    by_id = {c["category_id"]: c for c in cats}
    assert housing.id in by_id and rent.id in by_id
    # Rent's 3mo avg should reflect the $1000 settled last month.
    assert by_id[rent.id]["last_3mo_avg_actual"] > 0
    # Housing's 3mo avg must be ZERO — its only would-be source is
    # Rent's transactions, which now belong to Rent's own row.
    assert by_id[housing.id]["last_3mo_avg_actual"] == 0.0, (
        f"Housing row should not double-count Rent's transactions; "
        f"got {by_id[housing.id]['last_3mo_avg_actual']}"
    )


@pytest.mark.asyncio
async def test_unbudgeted_child_still_rolls_up_into_parent(
    db: AsyncSession,
    org: Organization,
    user: User,
    period: BillingPeriod,
):
    """When the child has NO budget of its own, the parent's facts
    row should include the child's transactions. This pins the
    rollup-for-unbudgeted-children behavior that the duplicate-skip
    guard must not break.
    """
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)

    housing = Category(
        org_id=org.id, name="Housing", type=CategoryType.EXPENSE, parent_id=None,
    )
    db.add(housing)
    await db.commit()
    rent = Category(
        org_id=org.id, name="Rent", type=CategoryType.EXPENSE, parent_id=housing.id,
    )
    db.add(rent)
    await db.commit()

    # Parent ONLY has a budget; child does not.
    db.add(Budget(
        org_id=org.id, category_id=housing.id,
        amount=Decimal("2000.00"),
        period_start=period.start_date, period_end=period.end_date,
    ))
    await db.commit()

    await _seed_history(
        db, org=org, user=user, category=rent,
        amount=Decimal("900.00"), settled=last_month,
    )

    captured_payloads: list[dict] = []

    async def fake_call(*args, messages, **kw):
        for m in messages:
            content = m.get("content", "")
            if "categories" in content and "category_id" in content:
                import ast, re
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match:
                    captured_payloads.append(ast.literal_eval(match.group()))
        return _make_structured_result(
            {"summary": "ok", "suggestions": []}
        )

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        await budget_rebalance_service.suggest_rebalance(db, org_id=org.id)

    cats = captured_payloads[0]["categories"]
    by_id = {c["category_id"]: c for c in cats}
    assert housing.id in by_id
    # Rent has no own facts row (no budget on Rent).
    assert rent.id not in by_id
    # Housing's facts row picks up Rent's $900 last month.
    assert by_id[housing.id]["last_3mo_avg_actual"] > 0
