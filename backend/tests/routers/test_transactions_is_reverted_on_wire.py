"""TBD-309 -- ``is_reverted`` on the transactions wire contract.

A row in ``skipped`` or ``rejected`` has had its amount REVERTED out of
``accounts.balance`` and sits outside every reportable aggregate. Until this
ticket the transactions surface could not tell: neither ``TransactionResponse``
nor the frontend ``Transaction`` type carried anything about it, so such a row
rendered as an ordinary transaction AND was offered a promote-to-recurring
affordance the server refuses (``promote_to_recurring``), a live violation of
the standing TBD-289 rule "no affordance is offered that the server will
refuse".

⚠ WHY A DERIVED BOOLEAN AND NOT THE ENUM. The ticket asks for
``reconciliation_state`` on the wire. Shipping the enum would require the
client to carry its own copy of ``REVERTED_RECONCILIATION_STATES`` in
TypeScript -- a second copy of the roster, in a second language, with no shared
declaration and no gate able to diff them. The day the roster gains a third
member the backend would drop the row from every aggregate while the client
kept rendering it as ordinary and kept offering promote, with BOTH suites
green. The derived boolean keeps the roster in exactly one language and makes
that drift unrepresentable: the wire carries the roster's RESULT, not its
inputs.

Two further reasons the enum is wrong on THIS surface:

* The ledger's copy must never assert a cause (TBD-289's copy fence). Handing
  it the words "skipped" / "rejected" is an invitation to write exactly that.
* The "Matched" badge is keyed on ``linked_transaction_id`` + mutuality, NOT on
  state, and must stay that way: ``MATCHED``'s only legal successor is
  ``ACCEPTED``, so a reconcile-matched row in a closed batch sits at
  ``accepted`` while still carrying its one-way link. A state field on this
  surface invites a future reader to rekey that badge and break it silently for
  every closed batch.

``ReconciliationRow`` keeps the enum. That surface's job IS naming and
transitioning states; the ledger's job is to not lie and to not offer refused
actions.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization, Transaction, User
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.routers.transactions import router as transactions_router
from app.schemas.import_reconciliation import ReconciliationState
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.transaction_filters import (
    REVERTED_RECONCILIATION_STATES,
    non_reverted_transaction_filter,
)

TX_DATE = date(2026, 5, 10)


@pytest_asyncio.fixture
async def session_factory():
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


def make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    @app.exception_handler(NotFoundError)
    async def _nf(_req, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(_req, exc: ValidationError):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(_req, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(transactions_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="tester",
            email="t@example.com",
            password_hash="x",
            is_superadmin=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id,
            name="Acct",
            account_type_id=at.id,
            balance=Decimal("1000.00"),
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
            currency="EUR",
        )
        cat = Category(org_id=org.id, name="Cat", slug="cat", type=CategoryType.BOTH)
        db.add_all([acct, cat])
        await db.commit()
        return {"org_id": org.id, "account_id": acct.id, "category_id": cat.id}


async def _make_tx(
    factory,
    seed: dict,
    *,
    state: str,
    is_manual_adjustment: bool = False,
) -> int:
    """Write the reconciliation state DIRECTLY so every roster member is
    constructible, including members no transition reaches from a fresh row."""
    async with factory() as db:
        tx = Transaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=seed["category_id"],
            description=f"row-{state}",
            amount=Decimal("10.00"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=TX_DATE,
            settled_date=TX_DATE,
            reconciliation_state=state,
            is_manual_adjustment=is_manual_adjustment,
        )
        db.add(tx)
        await db.commit()
        return tx.id


# ══ B3: the roster is imported, never re-literalled ═════════════════════════


def test_reverted_roster_is_the_same_object_as_the_private_tuple():
    """B3 (fence). ``REVERTED_RECONCILIATION_STATES`` must stay an ALIAS of
    ``_RECON_EXCLUDED_STATES``, not a second literal that happens to match.

    KILLS: re-declaring the roster as its own tuple. Equality would still pass;
    identity is what pins "one roster, two names".
    """
    from app.services import transaction_filters as tf

    assert tf.REVERTED_RECONCILIATION_STATES is tf._RECON_EXCLUDED_STATES


def test_reverted_roster_membership_tripwire():
    """B3b (tripwire, deliberately a restatement). If the roster grows this
    fails, forcing a conscious look at the ledger copy and the affordance rules
    before the new member reaches users.

    This never crosses a language boundary, so it is a REVIEW PROMPT, not a
    second definition. It is the only place in this file that names members.
    """
    assert set(REVERTED_RECONCILIATION_STATES) == {"skipped", "rejected"}


# ══ B2: every roster member, checked against the SQL, not against the roster ═


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [s.value for s in ReconciliationState])
async def test_is_reverted_agrees_with_the_production_sql_predicate(
    session_factory, state
):
    """B2 (fence). For EVERY member of ``ReconciliationState``, the serialized
    ``is_reverted`` flag must agree with the independent SQL implementation
    ``non_reverted_transaction_filter()``.

    ⚠ WHAT THIS DOES AND DOES NOT PIN -- read before trusting it.

    An earlier revision of this docstring claimed the SQL comparison made the
    two sides "independent implementations". That was FALSE and is withdrawn:
    ``non_reverted_transaction_filter()`` is one line over
    ``_RECON_EXCLUDED_STATES``, and ``REVERTED_RECONCILIATION_STATES`` is the
    SAME OBJECT (test above). Both sides bottom out in one tuple; only the
    evaluation mechanism differs, SQL ``NOT IN`` versus Python ``in``.
    Measured: shrink the roster to ``("skipped",)`` and all seven cases here
    stay GREEN. A fence that advertises anti-vacuity it does not have is worse
    than a plain one, because the next reader trusts the claim.

    What it DOES pin, and what nothing else does: the SQL clause itself.
    Mutate ``non_reverted_transaction_filter`` to ``!= "skipped"`` and the
    ``rejected`` case goes red. It also pins the derivation against the wrong
    column, an inverted flag, and the field missing from ``to_response``.

    What pins the ROSTER is the tripwire above plus the byte-identical rollup
    control, which reddens on all four snapshots when the roster changes.

    KILLS: mutating ``non_reverted_transaction_filter``; deriving from the
    wrong column; inverting the flag; omitting the field from ``to_response``.
    """
    from app.services import transaction_service

    seed = await _seed(session_factory)
    tx_id = await _make_tx(session_factory, seed, state=state)

    async with session_factory() as db:
        tx = await db.scalar(
            select(Transaction)
            .options(*transaction_service._load_opts())
            .where(Transaction.id == tx_id)
        )
        body = transaction_service.to_response(tx)
        survives_sql = await db.scalar(
            select(Transaction.id).where(
                Transaction.id == tx_id, non_reverted_transaction_filter()
            )
        )

    assert body.is_reverted is (survives_sql is None), (
        f"state={state!r}: wire flag is_reverted={body.is_reverted} disagrees "
        f"with non_reverted_transaction_filter() (row "
        f"{'survives' if survives_sql else 'is dropped'})"
    )


@pytest.mark.asyncio
async def test_manual_adjustment_is_not_reverted(session_factory):
    """B2b (fence). THE CONFLATION FENCE. A manual balance adjustment is
    excluded from ``reportable_transaction_filter`` but is deliberately KEPT by
    ``balance_contribution_filter`` and counted by ``reconcile_account`` -- its
    amount IS inside ``accounts.balance``.

    KILLS: naming or deriving this flag as "excluded from totals". Such a flag
    would be True here, and the indicator's copy asserts "its amount is not in
    your account balance" -- a falsehood shown to the user.
    """
    from app.services import transaction_service

    seed = await _seed(session_factory)
    tx_id = await _make_tx(
        session_factory, seed, state="accepted", is_manual_adjustment=True
    )

    async with session_factory() as db:
        tx = await db.scalar(
            select(Transaction)
            .options(*transaction_service._load_opts())
            .where(Transaction.id == tx_id)
        )
        assert transaction_service.to_response(tx).is_reverted is False


# ══ B1: the field survives a ROUTE, not just the funnel ════════════════════


@pytest.mark.asyncio
async def test_is_reverted_survives_list_detail_and_put_routes(session_factory):
    """B1 (fence). The flag must reach the client through every route the page
    actually uses, not merely through ``to_response`` in isolation.

    ⚠ Records the PATH, not the item: a fence covering only the list route
    certifies two routes it never touched.

    On the ``PUT``: the transactions page does NOT currently consume that body
    (it splices only ``recurring_id`` from the promote response and then
    refetches), so this is not protecting a live client path today -- do not
    justify it that way. It is protecting the CONTRACT. ``PUT`` returns a
    ``TransactionResponse`` on a PAT-reachable endpoint, so a third-party
    consumer sees the same shape as the list, and a row that lost the field on
    one route only would be a silent inconsistency in the wire contract rather
    than a rendering bug.

    KILLS: adding the field to the Pydantic model but not passing it in
    ``to_response``; covering the list route only.
    """
    seed = await _seed(session_factory)
    reverted_id = await _make_tx(session_factory, seed, state="skipped")
    ordinary_id = await _make_tx(session_factory, seed, state="accepted")

    with TestClient(make_app(session_factory)) as client:
        listing = client.get("/api/v1/transactions")
        assert listing.status_code == 200, listing.text
        by_id = {row["id"]: row for row in listing.json()["items"]}
        assert by_id[reverted_id]["is_reverted"] is True
        assert by_id[ordinary_id]["is_reverted"] is False

        detail = client.get(f"/api/v1/transactions/{reverted_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["is_reverted"] is True

        # The PUT body is what the page writes back into its row state.
        put = client.put(
            f"/api/v1/transactions/{ordinary_id}", json={"description": "edited"}
        )
        assert put.status_code == 200, put.text
        assert put.json()["is_reverted"] is False
