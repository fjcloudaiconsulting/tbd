"""BE-28 on REAL MySQL: turning merchant sharing off and an import's dictionary
vote are exclusive, in both orders, and concurrent withdrawals lose no
decrement.

Run with MERCHANT_DICTIONARY_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. The fixture deletes
the rows these tests write. SQLite has no row locks, so the unit half of this
fence (the re-check and the ordered locking reads) is in
``test_category_rules_service.py`` and ``test_import_execute_with_rules.py``.

The opt-out side is what ``PUT /api/v1/settings`` does for
``share_merchant_data=false``: write the row, flush it (taking its lock), then
withdraw the org's votes, one commit. The vote side is what each chunk of
``execute_import`` does: re-read the setting under its lock, then record the vote.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.category import Category, CategoryType
from app.models.merchant_dictionary import MerchantDictionaryEntry, MerchantDictionaryVote
from app.models.settings import OrgSetting
from app.models.user import Organization
from app.services import category_rules_service

URL = os.environ.get("MERCHANT_DICTIONARY_MYSQL_URL")
if not URL:
    pytestmark = pytest.mark.skipif(True, reason="MERCHANT_DICTIONARY_MYSQL_URL not set")

_CREATED: dict[str, list] = {"orgs": [], "tokens": []}


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(URL)
    _CREATED["orgs"].clear()
    _CREATED["tokens"].clear()
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        async with eng.begin() as conn:
            for org_id in _CREATED["orgs"]:
                for sql in (
                    "DELETE FROM merchant_dictionary_votes WHERE org_id = :i",
                    "DELETE FROM category_rules WHERE org_id = :i",
                    "DELETE FROM categories WHERE org_id = :i",
                    "DELETE FROM org_settings WHERE org_id = :i",
                    "DELETE FROM organizations WHERE id = :i",
                ):
                    await conn.execute(text(sql), {"i": org_id})
            for token in _CREATED["tokens"]:
                await conn.execute(
                    text("DELETE FROM merchant_dictionary WHERE normalized_token = :t"),
                    {"t": token},
                )
        await eng.dispose()


async def _token(f, *, vote_count: int = 0) -> str:
    token = f"RACE{uuid.uuid4().hex[:8].upper()}"
    async with f() as db:
        db.add(MerchantDictionaryEntry(normalized_token=token, category_slug="groceries",
                                       is_seed=True, vote_count=vote_count))
        await db.commit()
    _CREATED["tokens"].append(token)
    return token


async def _sharing_org(f) -> tuple[int, int]:
    async with f() as db:
        org = Organization(name=f"Race {uuid.uuid4()}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(OrgSetting(org_id=org.id, key="share_merchant_data", value="true"))
        groceries = Category(org_id=org.id, name="Groceries", slug="groceries",
                             is_system=True, type=CategoryType.EXPENSE)
        db.add(groceries)
        await db.commit()
    _CREATED["orgs"].append(org.id)
    return org.id, groceries.id


async def _vote(db: AsyncSession, org_id: int, category_id: int, token: str) -> None:
    if await category_rules_service.share_merchant_data_still_enabled(db, org_id):
        await category_rules_service.record_shared_vote(
            db, org_id=org_id, description=f"POS {token} *0001", category_id=category_id,
        )


async def _opt_out(db: AsyncSession, org_id: int) -> None:
    setting = await db.scalar(select(OrgSetting).where(
        OrgSetting.org_id == org_id, OrgSetting.key == "share_merchant_data"))
    setting.value = "false"
    await db.flush()
    await category_rules_service.withdraw_shared_votes(db, org_id=org_id)


async def _state(f, token: str) -> tuple[int, int]:
    async with f() as db:
        entry = await db.scalar(select(MerchantDictionaryEntry).where(
            MerchantDictionaryEntry.normalized_token == token))
        votes = await db.scalar(
            select(func.count()).select_from(MerchantDictionaryVote)
            .where(MerchantDictionaryVote.entry_id == entry.id))
        return entry.vote_count, votes


async def test_a_vote_waits_for_an_opt_out_in_progress(factory):
    """Opt-out holds the row; the import waits, then sees sharing off.
    Wrong implementation: voting on the first, unlocked read."""
    token = await _token(factory)
    org_id, groceries = await _sharing_org(factory)
    async with factory() as opt_out, factory() as importer:
        await _opt_out(opt_out, org_id)

        async def _import():
            await _vote(importer, org_id, groceries, token)
            await importer.commit()

        task = asyncio.create_task(_import())
        await asyncio.sleep(0.5)
        assert not task.done(), "the vote did not wait for the opt-out's row lock"
        await opt_out.commit()
        await asyncio.wait_for(task, timeout=10)
    assert await _state(factory, token) == (0, 0)


async def test_an_opt_out_withdraws_a_vote_that_committed_first(factory):
    """The import holds the row; the opt-out waits, then withdraws the vote it
    could not see when it started. Wrong implementation: an unlocked
    withdrawal read, which misses the newly committed row."""
    token = await _token(factory, vote_count=3)
    org_id, groceries = await _sharing_org(factory)
    async with factory() as importer, factory() as opt_out:
        await _vote(importer, org_id, groceries, token)

        async def _opt_out_and_commit():
            await _opt_out(opt_out, org_id)
            await opt_out.commit()

        task = asyncio.create_task(_opt_out_and_commit())
        await asyncio.sleep(0.5)
        assert not task.done(), "the opt-out did not wait for the vote's row lock"
        await importer.commit()
        await asyncio.wait_for(task, timeout=10)
    assert await _state(factory, token) == (3, 0)


async def test_concurrent_opt_outs_lose_no_decrement(factory):
    """Several orgs that voted for the same entries opt out at once: every
    decrement lands and no withdrawal deadlocks (entries are updated in
    ``entry_id`` order). Wrong implementation: a read-modify-write of
    ``vote_count``, or entries locked in differing orders."""
    tokens = [await _token(factory, vote_count=2) for _ in range(3)]
    orgs = [await _sharing_org(factory) for _ in range(4)]
    for org_id, groceries in orgs:
        async with factory() as db:
            # Vote in a different order per org.
            for token in (tokens if org_id % 2 else list(reversed(tokens))):
                await _vote(db, org_id, groceries, token)
            await db.commit()
    for token in tokens:
        assert await _state(factory, token) == (6, 4)

    async def _opt_out_and_commit(org_id: int) -> None:
        async with factory() as db:
            await _opt_out(db, org_id)
            await db.commit()

    await asyncio.wait_for(
        asyncio.gather(*(_opt_out_and_commit(org_id) for org_id, _ in orgs)), timeout=30
    )
    for token in tokens:
        assert await _state(factory, token) == (2, 0)
