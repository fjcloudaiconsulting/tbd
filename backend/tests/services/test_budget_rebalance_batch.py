"""TBD-461 — the atomic zero-sum rebalance service.

Fence ids match specs/2026-09-28-tbd-461-zero-sum-rebalance.md "Backend" list.
Every fixture seeds a ``BillingPeriod`` row at ``period_start``, or
``list_budgets`` (the read-back path) returns ``[]`` and the assertions on the
returned rows are vacuous.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.user import Organization
from app.schemas.budget import BudgetRebalanceItem
from app.services import budget_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

TODAY = datetime.date.today()


def _d(offset: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=offset)


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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_org(factory, org_id: int = 1) -> None:
    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()


async def _add_period(factory, org_id: int, start: datetime.date) -> None:
    async with factory() as db:
        db.add(BillingPeriod(org_id=org_id, start_date=start, end_date=None))
        await db.commit()


async def _add_category(factory, org_id: int, name: str) -> int:
    async with factory() as db:
        cat = Category(org_id=org_id, name=name, type=CategoryType.EXPENSE)
        db.add(cat)
        await db.commit()
        return cat.id


async def _add_budget(
    factory, org_id: int, category_id: int, start: datetime.date, amount: str,
) -> int:
    async with factory() as db:
        b = Budget(
            org_id=org_id, category_id=category_id, amount=Decimal(amount),
            period_start=start, period_end=None,
        )
        db.add(b)
        await db.commit()
        return b.id


async def _amounts(factory, budget_ids: list[int]) -> dict[int, Decimal]:
    async with factory() as db:
        rows = (
            await db.execute(select(Budget.id, Budget.amount).where(Budget.id.in_(budget_ids)))
        ).all()
    return {i: a for i, a in rows}


# ── F-B1 fence (atomicity) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb1_stale_third_item_leaves_the_first_two_unchanged(session_factory):
    """3 items, the 3rd stale -> 409; rows 1-2 unchanged. Kills a per-row commit."""
    org_id = 1
    await _seed_org(session_factory, org_id)
    await _add_period(session_factory, org_id, _d(0))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_b = await _add_category(session_factory, org_id, "B")
    cat_c = await _add_category(session_factory, org_id, "C")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    b = await _add_budget(session_factory, org_id, cat_b, _d(0), "100.00")
    c = await _add_budget(session_factory, org_id, cat_c, _d(0), "100.00")

    # Row c changes underneath us before the call.
    async with session_factory() as db:
        row = (await db.execute(select(Budget).where(Budget.id == c))).scalar_one()
        row.amount = Decimal("50.00")
        await db.commit()

    items = [
        BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
        BudgetRebalanceItem(budget_id=b, expected_amount=Decimal("100.00"), amount=Decimal("90.00")),
        BudgetRebalanceItem(budget_id=c, expected_amount=Decimal("100.00"), amount=Decimal("100.00")),
    ]
    async with session_factory() as db:
        with pytest.raises(ConflictError):
            await budget_service.rebalance_budgets(db, org_id, items)

    amounts = await _amounts(session_factory, [a, b, c])
    assert amounts[a] == Decimal("100.00")
    assert amounts[b] == Decimal("100.00")
    assert amounts[c] == Decimal("50.00")


# ── F-B2 fence (server net-zero) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb2_server_rejects_a_non_zero_net_even_if_client_thinks_it_balances(
    session_factory,
):
    org_id = 1
    await _seed_org(session_factory, org_id)
    await _add_period(session_factory, org_id, _d(0))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_b = await _add_category(session_factory, org_id, "B")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    b = await _add_budget(session_factory, org_id, cat_b, _d(0), "100.00")

    items = [
        BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
        BudgetRebalanceItem(budget_id=b, expected_amount=Decimal("100.00"), amount=Decimal("90.01")),
    ]
    async with session_factory() as db:
        with pytest.raises(ValidationError) as exc:
            await budget_service.rebalance_budgets(db, org_id, items)
    assert exc.value.detail == "Changes must net to zero."

    amounts = await _amounts(session_factory, [a, b])
    assert amounts[a] == Decimal("100.00")
    assert amounts[b] == Decimal("100.00")


# ── F-B3 fence (stale but cancelling) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb3_stale_identity_map_conflicts_even_when_payload_nets_to_zero(
    session_factory,
):
    """Session 1 opens; session 2 commits a change; session 1's stale
    identity map must still see the 409, thanks to populate_existing."""
    org_id = 1
    await _seed_org(session_factory, org_id)
    await _add_period(session_factory, org_id, _d(0))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_b = await _add_category(session_factory, org_id, "B")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    b = await _add_budget(session_factory, org_id, cat_b, _d(0), "100.00")

    async with session_factory() as db1:
        # Load rows into session 1's identity map first. The strong reference
        # is kept (not just discarded) so the identity map's weakrefs cannot
        # be garbage-collected before the call below reuses them.
        primed = (
            await db1.execute(select(Budget).where(Budget.id.in_([a, b])))
        ).scalars().all()
        assert len(primed) == 2

        # Session 2 changes both rows behind session 1's back.
        async with session_factory() as db2:
            rows = (
                await db2.execute(select(Budget).where(Budget.id.in_([a, b])))
            ).scalars().all()
            for row in rows:
                row.amount = Decimal("120.00") if row.id == a else Decimal("80.00")
            await db2.commit()

        items = [
            BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
            BudgetRebalanceItem(budget_id=b, expected_amount=Decimal("100.00"), amount=Decimal("90.00")),
        ]
        with pytest.raises(ConflictError) as exc:
            await budget_service.rebalance_budgets(db1, org_id, items)
        assert exc.value.code == "budget_changed"


# ── F-B4 fence (duplicate) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb4_duplicate_budget_id_rejected_at_schema_level():
    from app.schemas.budget import BudgetRebalanceRequest

    cat_a = 1
    items = [
        BudgetRebalanceItem(budget_id=cat_a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
        BudgetRebalanceItem(budget_id=cat_a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
        BudgetRebalanceItem(budget_id=2, expected_amount=Decimal("50.00"), amount=Decimal("30.00")),
    ]
    with pytest.raises(ValueError, match="uplicate"):
        BudgetRebalanceRequest(items=items)


# ── F-B5 fence (cross-org) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb5_cross_org_budget_id_is_404_and_untouched(session_factory):
    org_id = 1
    other_org_id = 2
    await _seed_org(session_factory, org_id)
    await _seed_org(session_factory, other_org_id)
    await _add_period(session_factory, org_id, _d(0))
    await _add_period(session_factory, other_org_id, _d(0))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_other = await _add_category(session_factory, other_org_id, "Other")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    other = await _add_budget(session_factory, other_org_id, cat_other, _d(0), "100.00")

    items = [
        BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
        BudgetRebalanceItem(budget_id=other, expected_amount=Decimal("100.00"), amount=Decimal("90.00")),
    ]
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await budget_service.rebalance_budgets(db, org_id, items)

    amounts = await _amounts(session_factory, [a, other])
    assert amounts[a] == Decimal("100.00")
    assert amounts[other] == Decimal("100.00")


# ── F-B6 fence (mixed periods) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb6_mixed_periods_rejected(session_factory):
    org_id = 1
    await _seed_org(session_factory, org_id)
    await _add_period(session_factory, org_id, _d(0))
    await _add_period(session_factory, org_id, _d(31))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_b = await _add_category(session_factory, org_id, "B")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    b = await _add_budget(session_factory, org_id, cat_b, _d(31), "100.00")

    items = [
        BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("110.00")),
        BudgetRebalanceItem(budget_id=b, expected_amount=Decimal("100.00"), amount=Decimal("90.00")),
    ]
    async with session_factory() as db:
        with pytest.raises(ValidationError) as exc:
            await budget_service.rebalance_budgets(db, org_id, items)
    assert exc.value.detail == "All budgets must be in the same period."


# ── F-B7 fence (floor) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb7_draining_to_zero_is_accepted_and_stored(session_factory):
    org_id = 1
    await _seed_org(session_factory, org_id)
    await _add_period(session_factory, org_id, _d(0))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_b = await _add_category(session_factory, org_id, "B")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    b = await _add_budget(session_factory, org_id, cat_b, _d(0), "0.00")

    items = [
        BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("0.00")),
        BudgetRebalanceItem(budget_id=b, expected_amount=Decimal("0.00"), amount=Decimal("100.00")),
    ]
    async with session_factory() as db:
        result = await budget_service.rebalance_budgets(db, org_id, items)

    amounts = await _amounts(session_factory, [a, b])
    assert amounts[a] == Decimal("0.00")
    assert amounts[b] == Decimal("100.00")
    assert {r.id for r in result} == {a, b}


# ── F-B8 fence (precision) ────────────────────────────────────────────────────


def test_fb8_three_decimal_places_rejected_at_schema_level():
    with pytest.raises(ValueError):
        BudgetRebalanceItem(
            budget_id=1, expected_amount=Decimal("100.000"), amount=Decimal("100.005")
        )


# ── G-B10 guard ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gb10_all_unchanged_payload_is_a_noop_returning_the_period(session_factory):
    org_id = 1
    await _seed_org(session_factory, org_id)
    await _add_period(session_factory, org_id, _d(0))
    cat_a = await _add_category(session_factory, org_id, "A")
    cat_b = await _add_category(session_factory, org_id, "B")
    cat_c = await _add_category(session_factory, org_id, "C")
    a = await _add_budget(session_factory, org_id, cat_a, _d(0), "100.00")
    b = await _add_budget(session_factory, org_id, cat_b, _d(0), "50.00")
    # Untouched third budget in the same period: kills a "return only the
    # locked rows" mutant, since only `a` and `b` are in the payload.
    c = await _add_budget(session_factory, org_id, cat_c, _d(0), "25.00")

    items = [
        BudgetRebalanceItem(budget_id=a, expected_amount=Decimal("100.00"), amount=Decimal("100.00")),
        BudgetRebalanceItem(budget_id=b, expected_amount=Decimal("50.00"), amount=Decimal("50.00")),
    ]
    async with session_factory() as db:
        result = await budget_service.rebalance_budgets(db, org_id, items)

    assert {r.id for r in result} == {a, b, c}
    amounts = await _amounts(session_factory, [a, b, c])
    assert amounts[a] == Decimal("100.00")
    assert amounts[b] == Decimal("50.00")
    assert amounts[c] == Decimal("25.00")
