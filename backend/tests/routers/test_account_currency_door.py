"""The currency door: an org cannot acquire a second currency (TBD-325, PR 1).

Spec: ``specs/2026-09-08-tbd-325-currency-door.md``.

Why this ships BEFORE the aggregate fix
---------------------------------------
Every period aggregate in the product is currency-blind — ``transactions`` has
no currency column, currency lives only on ``accounts.currency``, and the
aggregates never join ``Account``. So a multi-currency org would see EUR and
USD added together as one number.

A production measurement on 2026-09-03 found **zero** multi-currency orgs: all
52 accounts across 19 orgs are EUR, every value length 3, no typos or case
variants. The defect is therefore entirely **prospective** — nobody is looking
at a wrong number today. That measurement inverted the sequencing: close the
door first, cheaply, then do the ``primary_currency`` migration and the
six-site scoping against a cohort that is still empty.

⚠ This is PREVENTION, not the fix. The aggregates remain currency-blind after
this file goes green. What changes is that an org can no longer *reach* the
broken state — by typo or otherwise. The scoping work is TBD-325 PR 2 and
TBD-304.

The door is one field on one endpoint
-------------------------------------
Measured while scoping: ``AccountCreate.currency`` is the ONLY writer.
``AccountUpdate`` has no ``currency`` field, so currency is already immutable
post-create, and ``accounts.py``'s other mention is ``_to_response`` — a read.
That is what makes this PR small.

⚠ THE TWO POSITIVE CONTROLS ARE LOAD-BEARING.
``test_a_valid_iso_code_is_accepted`` and
``test_the_same_currency_again_is_accepted`` both pass against **today's
unmodified code**, so neither is a fence on its own. They are here because
without them the two real fences are satisfied by "reject every currency" and
"reject every second account" respectively — implementations that would break
account creation entirely while this file stayed green.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Organization
from app.models.base import Base
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.security import hash_password
from tests.factories import make_test_app


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


@pytest_asyncio.fixture
async def seeded(session_factory) -> dict:
    """One org, one admin, one checking account type. NO accounts yet.

    Tests that need an existing currency create the first account through the
    API, so the fixture cannot accidentally seed a state the endpoint would
    have refused.
    """
    async with session_factory() as db:
        org = Organization(name="Currency Door Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        admin = User(
            org_id=org.id,
            username="admin",
            email="admin@door.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
        )
        db.add(admin)

        at_checking = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at_checking)
        await db.flush()
        await db.commit()
        return {
            "org_id": org.id,
            "user_id": admin.id,
            "account_type_id": at_checking.id,
        }


def _app_for(session_factory, username: str):
    """Resolve the real ``User`` row, as the sibling account tests do.

    ``make_test_app`` takes a CALLABLE for ``current_user``, not a dict; the
    callable receives the session factory and must return a live User.
    """

    async def resolve(factory) -> User:
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one()

    return make_test_app(
        session_factory,
        routers=accounts_router,
        current_user=resolve,
        override_session_factory=True,
    )


@pytest.fixture
def client(session_factory, seeded):
    with TestClient(_app_for(session_factory, "admin")) as c:
        yield c


def _create(client, seeded, currency: str, name: str = "Acct"):
    return client.post(
        "/api/v1/accounts",
        json={
            "name": name,
            "account_type_id": seeded["account_type_id"],
            "currency": currency,
        },
    )


# ── Fence 1: the code must be a real currency ───────────────────────────────


@pytest.mark.parametrize("bad", ["XYZ", "EU", "EURO", "123", "", "  ", "€"])
def test_an_unknown_currency_code_is_refused(client, seeded, bad):
    """F1. Kills 'no validation at all'.

    The frontend input is ``maxLength=3`` + ``toUpperCase()`` and nothing more,
    so a typo like ``EU`` or ``XYZ`` reaches the API today and silently becomes
    a second currency. ``maxLength`` is not validation: it is a client-side
    convenience an API caller never sees.
    """
    res = _create(client, seeded, bad)
    assert res.status_code == 422, (
        f"currency {bad!r} was accepted (got {res.status_code}): {res.text[:200]}"
    )


def test_a_valid_iso_code_is_accepted(client, seeded):
    """⚠ OVER-REACH CONTROL, not a fence. Passes against unmodified code.

    Without it, ``raise`` on every currency satisfies F1 while breaking account
    creation for everyone.
    """
    res = _create(client, seeded, "USD")
    assert res.status_code == 201, res.text
    assert res.json()["currency"] == "USD"


def test_a_lowercase_code_is_normalised_rather_than_refused(client, seeded):
    """F2. Kills a membership test against a set of uppercase codes.

    The browser uppercases before sending, so a naive ``code in ISO_4217``
    looks correct in the UI and rejects every direct API caller using the
    conventional lowercase form. Normalise, then validate.
    """
    res = _create(client, seeded, "usd")
    assert res.status_code == 201, res.text
    assert res.json()["currency"] == "USD"


def test_surrounding_whitespace_is_stripped_rather_than_refused(client, seeded):
    """F3. Kills validating before normalising.

    ``" EUR "`` is a realistic paste. Stripping after the membership test means
    it is refused; stripping before means it is accepted and stored clean.
    """
    res = _create(client, seeded, " eur ")
    assert res.status_code == 201, res.text
    assert res.json()["currency"] == "EUR"


# ── Fence 2: an org may hold exactly one currency ───────────────────────────


def test_the_first_account_may_be_any_valid_currency(client, seeded):
    """The door constrains the SECOND account, never the first.

    An org with no accounts has no currency to disagree with, so every valid
    code must be reachable here — otherwise this ships a hardcoded EUR product.
    """
    assert _create(client, seeded, "JPY", name="First").status_code == 201


def test_a_second_different_currency_is_refused(client, seeded):
    """⚠⚠ THE FENCE THIS PR EXISTS FOR. Kills 'ISO validation only'.

    ISO-4217 validation alone still lets an org hold EUR and USD — both are
    real currencies. That is precisely the state every period aggregate reports
    wrongly, so validating the code without constraining the SET would close
    the typo door and leave the deliberate door wide open.
    """
    assert _create(client, seeded, "EUR", name="First").status_code == 201
    res = _create(client, seeded, "USD", name="Second")
    assert res.status_code == 409, (
        f"an org acquired a second currency (got {res.status_code}): {res.text[:200]}"
    )


def test_the_refusal_names_the_currency_the_org_already_holds(client, seeded):
    """The message is the whole usability of the refusal.

    A bare 409 tells the user nothing about which currency they must match, and
    there is no org-currency setting anywhere in the UI to look it up.
    """
    _create(client, seeded, "EUR", name="First")
    res = _create(client, seeded, "USD", name="Second")
    assert "EUR" in res.text, res.text[:300]


def test_the_same_currency_again_is_accepted(client, seeded):
    """⚠ OVER-REACH CONTROL, not a fence. Passes against unmodified code.

    Without it, "refuse every account after the first" satisfies the fence
    above while making the product single-account.
    """
    assert _create(client, seeded, "EUR", name="First").status_code == 201
    assert _create(client, seeded, "EUR", name="Second").status_code == 201


def test_the_same_currency_in_a_different_case_is_accepted(client, seeded):
    """F4. Kills comparing the raw input against the stored value.

    ``"eur" != "EUR"`` as strings. If the org-currency comparison runs before
    normalisation, a lowercase second account is refused as a *different*
    currency — a false 409 on a correct request.
    """
    assert _create(client, seeded, "EUR", name="First").status_code == 201
    assert _create(client, seeded, "eur", name="Second").status_code == 201


async def test_a_refused_account_writes_no_row(client, seeded, session_factory):
    """⚠ A handler that inserted first and validated second would still 409.

    Without this, the door could return the right status while leaving the org
    in exactly the multi-currency state it exists to prevent.
    """
    _create(client, seeded, "EUR", name="First")
    _create(client, seeded, "USD", name="Second")
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Account.currency).where(Account.org_id == seeded["org_id"])
            )
        ).scalars().all()
    assert sorted(set(rows)) == ["EUR"], f"stored currencies: {rows}"


async def test_the_constraint_is_per_org_not_global(session_factory, seeded):
    """A global check would make the FIRST org's currency the product's.

    Org A takes EUR; org B must still be free to take USD. Needs its own org,
    user, account type and client — a version of this test that only touched
    one org would pass against a global implementation, which is why it is
    built the long way.
    """
    async with session_factory() as db:
        org_b = Organization(name="Other Org", billing_cycle_day=1)
        db.add(org_b)
        await db.flush()
        user_b = User(
            org_id=org_b.id,
            username="admin-b",
            email="admin@other.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
        )
        db.add(user_b)
        at_b = AccountType(
            org_id=org_b.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at_b)
        await db.flush()
        await db.commit()
        b = {
            "org_id": org_b.id,
            "user_id": user_b.id,
            "account_type_id": at_b.id,
        }

    with TestClient(_app_for(session_factory, "admin")) as ca:
        assert _create(ca, seeded, "EUR", name="A1").status_code == 201

    with TestClient(_app_for(session_factory, "admin-b")) as cb:
        res = _create(cb, b, "USD", name="B1")
    assert res.status_code == 201, (
        f"org B was refused USD because org A holds EUR — the constraint is "
        f"global, not per-org: {res.text[:200]}"
    )


# ── The service's own contract, exercised directly ──────────────────────────
#
# ⚠ WHY THIS EXISTS. A mutation run found that replacing
# ``normalise_currency(existing) != normalise_currency(currency)`` with a raw
# ``existing != currency`` left all 17 tests above GREEN. The reason is that
# ``AccountCreate``'s field validator already normalises, so through the API
# the service never sees un-normalised input and its own normalisation is
# unreachable defensive code.
#
# Unreachable-but-untested defence is how a guard rots: the next caller of this
# public helper — a bulk importer, a migration, an admin tool — will not pass
# through the pydantic layer, and the raw comparison would then refuse a
# matching currency as a "second" one. These drive the service directly so its
# contract is fenced independently of the one caller that happens to pre-clean
# its input.


async def test_service_accepts_a_matching_currency_in_any_form(
    session_factory, seeded
):
    """F5. Kills the raw string comparison the API path cannot see."""
    from app.services.currency_service import assert_org_currency_allows

    async with session_factory() as db:
        db.add(
            Account(
                org_id=seeded["org_id"],
                account_type_id=seeded["account_type_id"],
                name="Existing",
                currency="EUR",
            )
        )
        await db.commit()

    async with session_factory() as db:
        for form in ("EUR", "eur", " eur ", "Eur"):
            # Must not raise: every form denotes the currency the org holds.
            await assert_org_currency_allows(
                db, org_id=seeded["org_id"], currency=form
            )


async def test_service_still_refuses_a_genuinely_different_currency(
    session_factory, seeded
):
    """Over-reach control for the test above.

    Without it, ``return`` at the top of the service satisfies F5 while
    removing the door entirely.
    """
    from app.services.currency_service import assert_org_currency_allows
    from app.services.exceptions import ConflictError

    async with session_factory() as db:
        db.add(
            Account(
                org_id=seeded["org_id"],
                account_type_id=seeded["account_type_id"],
                name="Existing",
                currency="EUR",
            )
        )
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(ConflictError, match="EUR"):
            await assert_org_currency_allows(
                db, org_id=seeded["org_id"], currency="USD"
            )
