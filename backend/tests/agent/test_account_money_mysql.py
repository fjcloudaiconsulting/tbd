"""TBD-665: the account money edit's confirm on REAL MySQL.

Run with ACCOUNT_MONEY_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. The confirm's
re-preview locks the account ``FOR UPDATE`` on its session; the handler's
atomic path then locks it again. On a second session that waits on the first
(InnoDB row locks, which SQLite does not have). The fixture deletes the rows
these tests write.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Account, AccountType, Organization
from app.models.agent_pending_action import AgentPendingAction
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.feature_override import OrgFeatureOverride
from app.models.user import Role, User
from app.routers import accounts as accounts_router
from tests.agent.test_actions import _confirm, _invoke

URL = os.environ.get("ACCOUNT_MONEY_MYSQL_URL")
if not URL:
    # CI sets REQUIRED=1, so a missing URL there is red, never a skip.
    if os.environ.get("ACCOUNT_MONEY_MYSQL_REQUIRED") == "1":
        raise RuntimeError("ACCOUNT_MONEY_MYSQL_URL is required here")
    pytestmark = pytest.mark.skipif(True, reason="ACCOUNT_MONEY_MYSQL_URL not set")

_LOCKED = 3572  # ER_LOCK_NOWAIT: the row is locked by another transaction
_CREATED_ORGS: list[int] = []


@pytest_asyncio.fixture
async def eng():
    e = create_async_engine(URL)

    @event.listens_for(e.sync_engine, "connect")
    def _short_wait(dbapi_conn, _record):
        # A lock wait on itself fails in seconds, not innodb's default 50.
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION innodb_lock_wait_timeout = 5")
        cur.close()

    _CREATED_ORGS.clear()
    try:
        yield e
    finally:
        async with e.begin() as conn:
            for org_id in _CREATED_ORGS:
                for table in ("accounts", "account_types", "agent_pending_actions", "users",
                              "org_feature_overrides"):
                    await conn.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})
                await conn.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await e.dispose()


@pytest.fixture
def factory(eng):
    return async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory) -> dict:
    """An admin, and a credit card (balance 0) with one cycle payment."""
    async with factory() as db:
        org = Organization(name=f"Money {uuid.uuid4()}", billing_cycle_day=1, primary_currency="EUR")
        db.add(org)
        await db.flush()
        _CREATED_ORGS.append(org.id)
        admin = User(org_id=org.id, username=f"u{uuid.uuid4().hex[:12]}",
                     email=f"{uuid.uuid4().hex[:12]}@example.com", password_hash="x",
                     role=Role.ADMIN, is_active=True, email_verified=True)
        checking = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=False)
        card_type = AccountType(org_id=org.id, name="Card", slug="credit_card", is_system=False)
        db.add_all([admin, checking, card_type,
                    OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True)])
        await db.flush()
        card = Account(org_id=org.id, name="Card", account_type_id=card_type.id,
                       balance=Decimal("0.00"), currency="EUR", close_day=15,
                       credit_limit=Decimal("1000.00"))
        db.add(card)
        await db.flush()
        db.add(CcCyclePayment(account_id=card.id, period_anchor_year=2030, period_anchor_month=6,
                              amount=Decimal("80.00")))
        await db.commit()
        return {"admin": admin.id, "checking": checking.id, "card": card.id}


async def _locked_elsewhere(eng, account_id: int) -> bool:
    """Whether another transaction holds the account's row lock right now."""
    async with eng.connect() as conn:
        try:
            await conn.execute(
                text("SELECT id FROM accounts WHERE id = :i FOR UPDATE NOWAIT"), {"i": account_id}
            )
        except DBAPIError as exc:
            assert exc.orig.args[0] == _LOCKED, exc
            return True
        finally:
            await conn.rollback()
    return False


async def test_confirm_writes_under_the_preview_lock_without_waiting_on_it(eng, factory, monkeypatch):
    """FENCE (TBD-665). Wrong implementations: the handler's atomic path on a
    second session, which waits on the row lock the confirm's re-preview holds
    on its own (a lock wait timeout, so the confirm fails); or a confirm that
    releases the re-preview's lock before the write, so a concurrent change
    lands between the fingerprint check and the write."""
    s = await _seed(factory)
    args = {"account_id": s["card"], "account_type_id": s["checking"], "opening_balance": "50.00"}
    staged = await _invoke(factory, s["admin"], "accounts_update_money", args)
    assert {c["entity"] for c in staged["changes"]} == {"accounts", "cc_cycle_payments"}
    assert not await _locked_elsewhere(eng, s["card"]), "staging left the row locked"

    seen: list[tuple[str, bool]] = []
    real = accounts_router.apply_type_change_in_session

    async def probed(svc_db, **kw):
        seen.append(("before the write", await _locked_elsewhere(eng, s["card"])))
        out = await real(svc_db, **kw)
        seen.append(("after the write", await _locked_elsewhere(eng, s["card"])))
        return out

    monkeypatch.setattr(accounts_router, "apply_type_change_in_session", probed)
    done = await asyncio.wait_for(_confirm(factory, s["admin"], staged["action_id"]), timeout=30)

    assert done["status"] == "done"
    assert seen == [("before the write", True), ("after the write", True)]
    assert not await _locked_elsewhere(eng, s["card"]), "the confirm left the row locked"
    async with factory() as db:
        acct = await db.get(Account, s["card"])
        assert (acct.account_type_id, acct.close_day, acct.credit_limit) == (s["checking"], None, None)
        assert (acct.opening_balance, acct.balance) == (Decimal("50.00"), Decimal("50.00"))
        left = await db.scalar(
            select(func.count()).select_from(CcCyclePayment).where(CcCyclePayment.account_id == s["card"])
        )
        assert left == 0
        row = await db.get(AgentPendingAction, staged["action_id"])
        assert row.status.value == "done"

