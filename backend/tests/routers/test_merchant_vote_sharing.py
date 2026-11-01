"""BE-28 over HTTP: turning ``share_merchant_data`` off through the settings
routes withdraws the org's votes on the shared merchant dictionary, in the
same commit as the setting, as BE-18 does for tags. Turning it back on brings
no vote back; only a new confirm counts again. Service-level counting rules
are in ``tests/services/test_category_rules_service.py``.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.merchant_dictionary import MerchantDictionaryEntry, MerchantDictionaryVote
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.settings import router as settings_router
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.security import hash_password
from app.services import import_service
from tests.factories import make_test_app

# Stored counts: a legacy baseline the vote rows were added on top of.
_LIDL_BASELINE = 5


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


async def _seed(factory) -> dict:
    """The acting org (owner ``root``, sharing on) votes for LIDL and ALDI;
    another org votes for LIDL too."""
    async with factory() as db:
        mine = Organization(name="Mine", billing_cycle_day=1)
        other = Organization(name="Other", billing_cycle_day=1)
        db.add_all([mine, other])
        await db.flush()
        db.add(OrgSetting(org_id=mine.id, key="share_merchant_data", value="true"))
        user = User(org_id=mine.id, username="root", email="root@x.io",
                    password_hash=hash_password("pw-1234567"), role=Role.OWNER,
                    is_active=True, email_verified=True)
        groceries = Category(org_id=mine.id, name="Groceries", slug="groceries",
                             is_system=True, type=CategoryType.EXPENSE)
        at = AccountType(org_id=mine.id, name="Checking", slug="checking", is_system=True)
        lidl = MerchantDictionaryEntry(normalized_token="LIDL", category_slug="groceries",
                                       is_seed=True, vote_count=_LIDL_BASELINE + 2)
        aldi = MerchantDictionaryEntry(normalized_token="ALDI", category_slug="groceries",
                                       is_seed=True, vote_count=1)
        db.add_all([user, groceries, at, lidl, aldi])
        await db.flush()
        acc = Account(org_id=mine.id, name="Main", account_type_id=at.id,
                      balance=Decimal("0"))
        db.add(acc)
        db.add_all([
            MerchantDictionaryVote(entry_id=lidl.id, org_id=mine.id),
            MerchantDictionaryVote(entry_id=aldi.id, org_id=mine.id),
            MerchantDictionaryVote(entry_id=lidl.id, org_id=other.id),
        ])
        await db.commit()
        return {"org_id": mine.id, "other_id": other.id, "user_id": user.id,
                "account_id": acc.id, "groceries_id": groceries.id}


def _app(factory, user_id: int):
    async def resolve_user(session_factory) -> User:
        async with session_factory() as db:
            return (await db.execute(select(User).where(User.id == user_id))).scalar_one()

    return make_test_app(
        factory,
        routers=[settings_router],
        current_user=resolve_user,
        override_session_factory=True,
    )


async def _state(factory) -> tuple[dict[str, int], set[tuple[str, int]]]:
    async with factory() as db:
        counts = dict((await db.execute(
            select(MerchantDictionaryEntry.normalized_token, MerchantDictionaryEntry.vote_count)
        )).all())
        votes = set((await db.execute(
            select(MerchantDictionaryEntry.normalized_token, MerchantDictionaryVote.org_id)
            .join(MerchantDictionaryEntry,
                  MerchantDictionaryEntry.id == MerchantDictionaryVote.entry_id)
        )).all())
        return counts, votes


async def _confirm_lidl(factory, seeds: dict) -> None:
    async with factory() as db:
        await import_service.execute_import(db, org_id=seeds["org_id"], body=ImportConfirmRequest(
            account_id=seeds["account_id"],
            default_category_id=seeds["groceries_id"],
            file_name="test.csv",
            source_format="csv",
            rows=[ImportConfirmRow(
                row_number=1, date=datetime.date(2026, 5, 1),
                description="POS LIDL *9999", amount=Decimal("12.50"), type="expense",
                category_id=seeds["groceries_id"],
            )],
        ))


async def test_turning_merchant_sharing_off_withdraws_the_orgs_votes(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        res = client.put("/api/v1/settings",
                         json={"key": "share_merchant_data", "value": "false"})
    assert res.status_code == 200, res.text
    counts, votes = await _state(session_factory)
    assert counts == {"LIDL": _LIDL_BASELINE + 1, "ALDI": 0}
    assert votes == {("LIDL", seeds["other_id"])}


async def test_deleting_the_merchant_sharing_setting_withdraws_the_orgs_votes(
    session_factory,
):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        res = client.delete("/api/v1/settings/share_merchant_data")
    assert res.status_code == 204, res.text
    counts, votes = await _state(session_factory)
    assert counts == {"LIDL": _LIDL_BASELINE + 1, "ALDI": 0}
    assert votes == {("LIDL", seeds["other_id"])}


async def test_keeping_merchant_sharing_on_keeps_the_votes(session_factory):
    seeds = await _seed(session_factory)
    before = await _state(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        res = client.put("/api/v1/settings",
                         json={"key": "share_merchant_data", "value": "true"})
        other = client.put("/api/v1/settings",
                           json={"key": "something_else", "value": "false"})
    assert res.status_code == 200 and other.status_code == 200
    assert await _state(session_factory) == before


async def test_turning_merchant_sharing_back_on_restores_no_vote(session_factory):
    """Off then on: the withdrawn votes stay gone until the org confirms a
    dictionary suggestion again, which counts once."""
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        for value in ("false", "true"):
            res = client.put("/api/v1/settings",
                             json={"key": "share_merchant_data", "value": value})
            assert res.status_code == 200, res.text
    counts, votes = await _state(session_factory)
    assert counts == {"LIDL": _LIDL_BASELINE + 1, "ALDI": 0}
    assert votes == {("LIDL", seeds["other_id"])}

    await _confirm_lidl(session_factory, seeds)
    counts, votes = await _state(session_factory)
    assert counts == {"LIDL": _LIDL_BASELINE + 2, "ALDI": 0}
    assert votes == {("LIDL", seeds["other_id"]), ("LIDL", seeds["org_id"])}


async def test_an_import_after_turning_sharing_off_casts_no_vote(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        res = client.put("/api/v1/settings",
                         json={"key": "share_merchant_data", "value": "false"})
    assert res.status_code == 200, res.text
    await _confirm_lidl(session_factory, seeds)
    counts, votes = await _state(session_factory)
    assert counts == {"LIDL": _LIDL_BASELINE + 1, "ALDI": 0}
    assert votes == {("LIDL", seeds["other_id"])}
