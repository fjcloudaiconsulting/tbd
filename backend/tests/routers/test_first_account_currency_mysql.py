"""TBD-508 on REAL MySQL: concurrent first accounts in a fresh org neither
deadlock nor leave the org holding two currencies.

Run with ACCOUNTS_CURRENCY_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``;
ACCOUNTS_CURRENCY_MYSQL_REQUIRED=1 makes a missing URL a failure instead of a
skip. ``assert_org_currency_allows`` reads the org's accounts ``FOR UPDATE``;
on an empty org that is a gap lock, and two creates holding it deadlock on
each other's insert (error 1213, a 500). Locking the ``organizations`` row
first queues the second create behind the first. SQLite has no gap locks and
ignores ``FOR UPDATE``, so it can neither show the deadlock nor the fix.

Both assertions matter: catching the 1213 would make the 500s go away and
could let the org end up in two currencies.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account, AccountType
from app.models.user import Organization, Role, User
from app.routers.accounts import router as accounts_router
from app.services.admin_orgs_service import delete_org_cascade
from app.services.demo_seed_service import seed_org

URL = os.environ.get("ACCOUNTS_CURRENCY_MYSQL_URL")
if not URL:
    if os.environ.get("ACCOUNTS_CURRENCY_MYSQL_REQUIRED") == "1":
        raise RuntimeError(
            "ACCOUNTS_CURRENCY_MYSQL_REQUIRED=1 but ACCOUNTS_CURRENCY_MYSQL_URL is not set"
        )
    pytestmark = pytest.mark.skipif(True, reason="ACCOUNTS_CURRENCY_MYSQL_URL not set")

# The deadlock is a race: one trial does not always hit it. Before the fix,
# 2 concurrent creates deadlocked in about a quarter to a half of trials.
TRIALS = 20


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    eng = create_async_engine(URL)

    @event.listens_for(eng.sync_engine, "connect")
    def _short_wait(dbapi_conn, _record):
        # A lock wait that never resolves fails in seconds, not InnoDB's 50.
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION innodb_lock_wait_timeout = 10")
        cur.close()

    f = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    f.org_ids = []  # type: ignore[attr-defined]
    try:
        yield f
    finally:
        async with f() as db:
            for org_id in f.org_ids:  # type: ignore[attr-defined]
                await delete_org_cascade(db, org_id=org_id)
            await db.commit()
        await eng.dispose()


async def _fresh_org(f) -> tuple[User, int]:
    """An org with an owner and a checking type, and no accounts."""
    tag = secrets.token_hex(4)
    async with f() as db:
        org = Organization(name=f"first-account-race-{tag}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        f.org_ids.append(org.id)
        user = User(org_id=org.id, username=f"far-{tag}", email=f"far-{tag}@example.com",
                    password_hash="x", role=Role.OWNER, is_active=True, email_verified=True)
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add_all([user, at])
        await db.commit()
        return user, at.id


def _client(f, user: User) -> httpx.AsyncClient:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with f() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.include_router(accounts_router)

    @app.exception_handler(Exception)
    async def _500(_request: Request, exc: Exception) -> PlainTextResponse:
        # The 500 a user sees, carrying the error so a failure names the 1213.
        return PlainTextResponse(f"{type(exc).__name__}: {exc}"[:200], status_code=500)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _org_state(f, org_id: int) -> tuple[set[str], str | None]:
    async with f() as db:
        currencies = set((await db.execute(
            select(Account.currency).where(Account.org_id == org_id)
        )).scalars())
        primary = await db.scalar(
            select(Organization.primary_currency).where(Organization.id == org_id)
        )
    return currencies, primary


@pytest.mark.asyncio
async def test_concurrent_first_accounts_do_not_deadlock_or_mix_currencies(factory):
    failures = []
    for trial in range(TRIALS):
        user, type_id = await _fresh_org(factory)
        async with _client(factory, user) as client:
            results = await asyncio.gather(*(
                client.post("/api/v1/accounts", json={
                    "name": f"{cur} account", "account_type_id": type_id, "currency": cur,
                })
                for cur in ("EUR", "USD")
            ))
        codes = sorted(r.status_code for r in results)
        currencies, primary = await _org_state(factory, user.org_id)
        # One create wins; the other waits for it, sees its currency and is refused.
        if codes != [201, 409] or len(currencies) != 1 or primary not in currencies:
            failures.append((trial, codes, sorted(currencies), primary,
                             [r.text for r in results if r.status_code >= 500]))
    assert not failures, f"{len(failures)}/{TRIALS} trials failed: {failures}"


@pytest.mark.asyncio
async def test_concurrent_demo_seed_and_first_account_do_not_deadlock_or_mix(factory):
    """The onboarding seed is the other first-account path. It now locks the
    org row first, like a create, so the two queue rather than interleave.
    Before that the seed picked its currency from a plain read: a create
    committed after the seed's snapshot made it pick EUR and then be refused
    by its own currency check (measured 1 in 100 trials; no deadlock seen)."""
    failures = []
    for trial in range(TRIALS):
        user, type_id = await _fresh_org(factory)

        async def seed(org_id: int) -> str:
            async with factory() as db:
                try:
                    await seed_org(db, org_id)
                    await db.commit()
                    return "seeded"
                except Exception as e:  # noqa: BLE001 -- the outcome is the assertion
                    return f"{type(e).__name__}: {e}"[:200]

        async with _client(factory, user) as client:
            seeded, created = await asyncio.gather(seed(user.org_id), client.post("/api/v1/accounts", json={
                "name": "USD account", "account_type_id": type_id, "currency": "USD",
            }))
        currencies, primary = await _org_state(factory, user.org_id)
        # Either order is fine: the seed adopts USD if the create went first,
        # and a create after the seed is refused for disagreeing with EUR.
        if (seeded != "seeded" or created.status_code not in (201, 409)
                or len(currencies) != 1 or primary not in currencies):
            failures.append((trial, seeded, created.status_code, created.text if
                             created.status_code >= 500 else "", sorted(currencies), primary))
    assert not failures, f"{len(failures)}/{TRIALS} trials failed: {failures}"
