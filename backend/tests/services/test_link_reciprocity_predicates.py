"""Link-reciprocity predicates (TBD-280 / 281 / 282 / 293).

THE RULE: a link is a transfer link if, and only if, the partner links back.

Two fences live here:

* ``is_reciprocal_pair`` -- the "are these two rows ONE transfer pair?"
  predicate. Fails CLOSED.
* **F17**: parity between ``contributes_to_cached_balance`` (the Python
  sibling) and ``balance_contribution_filter`` (the SQL). Eight shapes.
  The one documented divergence cell is ``xfail(strict=True)`` so the
  fence turns RED if the divergence ever disappears -- a non-strict
  xfail would pass silently and be decoration.

No fixture here relies on id ``1``: every transaction is seeded with an
explicit id in the 7000 range.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.services.transaction_filters import (
    balance_contribution_filter,
    contributes_to_cached_balance,
    is_reciprocal_pair,
)


@pytest_asyncio.fixture
async def db_session():
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
    async with factory() as session:
        yield session
    await engine.dispose()


# ── Fixture ─────────────────────────────────────────────────────────────────
#
# Eight shapes, each with a named SUBJECT row (the one the predicates are
# evaluated on). Ids are explicit and offset from 1 so nothing can pass by
# accidentally matching the first-seeded row.

SUBJECT_IDS = {
    "null_link": 7001,
    "reciprocal": 7002,
    "one_way": 7004,
    "self_link": 7006,
    "cross_org_reciprocal": 7007,
    "cross_org_one_way": 7009,
    "rejected": 7011,
    "chain": 7012,
}


async def _seed_shapes(db: AsyncSession) -> dict:
    """Seed two orgs and the eight link shapes.

    Per §6.0.7 of the design: self-links and cross-org links need no raw
    SQL and no FK disabling -- a plain UPDATE after INSERT suffices,
    because the FK only constrains ``transactions.id``, not the org.
    """
    org_a = Organization(name="Primary", billing_cycle_day=1)
    org_b = Organization(name="Other", billing_cycle_day=1)
    db.add_all([org_a, org_b])
    await db.flush()

    at_a = AccountType(org_id=org_a.id, name="Checking", slug="checking", is_system=True)
    at_b = AccountType(org_id=org_b.id, name="Checking", slug="checking", is_system=True)
    db.add_all([at_a, at_b])
    await db.flush()

    acct_a = Account(
        org_id=org_a.id, name="A", account_type_id=at_a.id,
        balance=Decimal("500.00"), currency="EUR",
    )
    acct_b = Account(
        org_id=org_b.id, name="B", account_type_id=at_b.id,
        balance=Decimal("300.00"), currency="EUR",
    )
    db.add_all([acct_a, acct_b])
    await db.flush()

    cat_a = Category(
        org_id=org_a.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    cat_b = Category(
        org_id=org_b.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    db.add_all([cat_a, cat_b])
    await db.flush()

    def row(tx_id: int, *, org, acct, cat, amount: str, state: str = "accepted"):
        return Transaction(
            id=tx_id,
            org_id=org.id,
            account_id=acct.id,
            category_id=cat.id,
            description=f"row-{tx_id}",
            amount=Decimal(amount),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=date(2026, 5, 1),
            settled_date=date(2026, 5, 1),
            reconciliation_state=state,
        )

    in_a = dict(org=org_a, acct=acct_a, cat=cat_a)
    in_b = dict(org=org_b, acct=acct_b, cat=cat_b)

    rows = [
        row(7001, amount="1.00", **in_a),   # 1. link NULL
        row(7002, amount="2.00", **in_a),   # 2. reciprocal (subject)
        row(7003, amount="2.00", **in_a),   #    reciprocal partner
        row(7004, amount="4.00", **in_a),   # 3. one-way (subject)
        row(7005, amount="4.00", **in_a),   #    one-way target (links nowhere)
        row(7006, amount="8.00", **in_a),   # 4. self-link
        row(7007, amount="16.00", **in_a),  # 5. cross-org reciprocal (subject)
        row(7008, amount="16.00", **in_b),  #    cross-org reciprocal partner
        row(7009, amount="32.00", **in_a),  # 6. cross-org one-way (subject)
        row(7010, amount="32.00", **in_b),  #    cross-org one-way target
        row(7011, amount="64.00", state="rejected", **in_a),   # 7. rejected
        row(7012, amount="128.00", **in_a),  # 8. chain head (subject)
        row(7013, amount="128.00", **in_a),  #    chain middle
        row(7014, amount="128.00", **in_a),  #    chain tail
    ]
    db.add_all(rows)
    await db.flush()

    by_id = {r.id: r for r in rows}
    # Plain UPDATEs -- no raw SQL, no FK disabling needed.
    by_id[7002].linked_transaction_id = 7003
    by_id[7003].linked_transaction_id = 7002
    by_id[7004].linked_transaction_id = 7005
    by_id[7006].linked_transaction_id = 7006          # self-link
    by_id[7007].linked_transaction_id = 7008          # cross-org, mutual
    by_id[7008].linked_transaction_id = 7007
    by_id[7009].linked_transaction_id = 7010          # cross-org, one-way
    by_id[7012].linked_transaction_id = 7013          # chain A -> B -> C
    by_id[7013].linked_transaction_id = 7014
    await db.commit()

    return {"org_a_id": org_a.id, "org_b_id": org_b.id, "rows": by_id}


async def _load(db: AsyncSession, tx_id: int) -> Transaction:
    return await db.scalar(select(Transaction).where(Transaction.id == tx_id))


async def _resolve_partner(db: AsyncSession, tx: Transaction) -> Transaction | None:
    """Resolve the partner the way every production call site does:
    ORG-SCOPED. A cross-org link therefore resolves to ``None``."""
    if tx.linked_transaction_id is None:
        return None
    return await db.scalar(
        select(Transaction).where(
            Transaction.id == tx.linked_transaction_id,
            Transaction.org_id == tx.org_id,
        )
    )


# ── is_reciprocal_pair ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape,expected",
    [
        ("null_link", False),
        ("reciprocal", True),
        ("one_way", False),
        # A self-linked row is corrupt data containing exactly ONE row.
        # Treating it as a pair makes every two-row path double-count it.
        ("self_link", False),
        # Cross-org: partner resolves to None under org scoping, and the
        # org_id term refuses it even if a caller hands it over anyway.
        ("cross_org_reciprocal", False),
        ("cross_org_one_way", False),
        ("rejected", False),
        # Mechanism (partner HAS a link) is not the property (partner
        # links BACK).
        ("chain", False),
    ],
)
async def test_is_reciprocal_pair_shapes(db_session, shape, expected):
    await _seed_shapes(db_session)
    tx = await _load(db_session, SUBJECT_IDS[shape])
    partner = await _resolve_partner(db_session, tx)
    assert is_reciprocal_pair(tx, partner) is expected


@pytest.mark.asyncio
async def test_is_reciprocal_pair_refuses_a_cross_org_partner_handed_in_directly(
    db_session,
):
    """The org term is not dead weight in the Python predicate: a caller
    that resolves the partner WITHOUT org scoping must still be refused."""
    await _seed_shapes(db_session)
    tx = await _load(db_session, SUBJECT_IDS["cross_org_reciprocal"])
    unscoped_partner = await _load(db_session, 7008)
    assert unscoped_partner.linked_transaction_id == tx.id  # mutual...
    assert unscoped_partner.org_id != tx.org_id             # ...but foreign
    assert is_reciprocal_pair(tx, unscoped_partner) is False


@pytest.mark.asyncio
async def test_is_reciprocal_pair_is_argument_order_safe_on_a_transient_partner(
    db_session,
):
    """``tx.linked_transaction_id is not None`` is load-bearing: without
    it an unflushed partner (``id`` still None) makes ``partner.id ==
    tx.linked_transaction_id`` collapse to ``None == None`` -> True, and
    an UNLINKED row is reported as one leg of a transfer pair.

    The transient must itself point back at ``tx``, otherwise the final
    reciprocity term rejects it for an unrelated reason and the fence
    goes green against a mutant that removed the guard.
    """
    await _seed_shapes(db_session)
    tx = await _load(db_session, SUBJECT_IDS["null_link"])
    transient = Transaction(
        org_id=tx.org_id,
        account_id=tx.account_id,
        category_id=tx.category_id,
        description="transient",
        amount=Decimal("256.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1),
        linked_transaction_id=tx.id,
    )
    assert transient.id is None
    assert tx.linked_transaction_id is None
    assert is_reciprocal_pair(tx, transient) is False


# ── F17: Python sibling vs SQL parity ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "null_link",
        "reciprocal",
        "one_way",
        "self_link",
        "cross_org_reciprocal",
        pytest.param(
            "cross_org_one_way",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "DOCUMENTED DIVERGENCE: the Python predicate fails OPEN "
                    "whenever the partner is unresolvable (here: cross-org), "
                    "while the SQL has no org scoping and sees the one-way "
                    "link. Unreachable in production -- no writer produces a "
                    "cross-org link. strict=True so this fence turns RED if "
                    "the divergence is ever closed."
                ),
            ),
        ),
        "rejected",
        "chain",
    ],
)
async def test_contributes_to_cached_balance_matches_the_sql_filter(db_session, shape):
    """F17. Kills: parity drift between the Python sibling and the SQL.

    The chain shape is load-bearing. Without it the mutant
    ``partner.linked_transaction_id is not None`` -- MECHANISM (the
    partner has a link) instead of PROPERTY (the partner links BACK) --
    survives every other shape.
    """
    await _seed_shapes(db_session)
    subject_id = SUBJECT_IDS[shape]
    tx = await _load(db_session, subject_id)
    partner = await _resolve_partner(db_session, tx)

    python_answer = contributes_to_cached_balance(tx, partner)

    kept = set(
        (
            await db_session.scalars(
                select(Transaction.id).where(balance_contribution_filter())
            )
        ).all()
    )
    sql_answer = subject_id in kept

    assert python_answer is sql_answer, (
        f"shape={shape}: python={python_answer} sql={sql_answer}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape,expected",
    [
        ("null_link", True),
        ("reciprocal", True),
        ("one_way", False),
        # Kept, matching the SQL: the correlated EXISTS matches a row
        # against itself. This filter fails OPEN by design.
        ("self_link", True),
        ("cross_org_reciprocal", True),
        ("cross_org_one_way", True),   # fails OPEN -- see DIVERGENCE
        ("rejected", False),
        ("chain", False),
    ],
)
async def test_contributes_to_cached_balance_absolute_values(
    db_session, shape, expected
):
    """The parity fence above compares two implementations; if BOTH drifted
    the same way it would stay green. This pins the absolute answers."""
    await _seed_shapes(db_session)
    tx = await _load(db_session, SUBJECT_IDS[shape])
    partner = await _resolve_partner(db_session, tx)
    assert contributes_to_cached_balance(tx, partner) is expected
