"""BE-18 on REAL MySQL: opting out of tag sharing and contributing a tag are
exclusive, in both orders.

Run with TAG_DICTIONARY_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. The fixture deletes
the rows these tests write. SQLite has no row locks, so the unit half of this
fence (the re-check and the locking reads) is in
``test_tag_dictionary_hardening.py``.

The opt-out side is what ``PUT /api/v1/settings`` does for
``share_tag_data=false``: write the row, flush it (taking its lock), then
withdraw the org's contributions, one commit.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app._time import utcnow_naive
from app.models.settings import OrgSetting
from app.models.tag import Tag, TagDictionary, TagDictionaryContributor
from app.models.user import Organization
from app.services import tag_service

URL = os.environ.get("TAG_DICTIONARY_MYSQL_URL")
if not URL:
    pytestmark = pytest.mark.skipif(True, reason="TAG_DICTIONARY_MYSQL_URL not set")

_CREATED: dict[str, list] = {"orgs": [], "names": []}


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(URL)
    _CREATED["orgs"].clear()
    _CREATED["names"].clear()
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        async with eng.begin() as conn:
            for org_id in _CREATED["orgs"]:
                for sql in (
                    "DELETE FROM tag_dictionary_contributors WHERE contributor_org_id = :i",
                    "DELETE FROM tags WHERE org_id = :i",
                    "DELETE FROM org_settings WHERE org_id = :i",
                    "DELETE FROM organizations WHERE id = :i",
                ):
                    await conn.execute(text(sql), {"i": org_id})
            for name in _CREATED["names"]:
                await conn.execute(
                    text("DELETE FROM tag_dictionary WHERE name_normalized = :n"), {"n": name}
                )
        await eng.dispose()


async def _sharing_org(f) -> tuple[int, str]:
    name = f"race{uuid.uuid4().hex[:8]}"
    async with f() as db:
        org = Organization(name=f"Race {uuid.uuid4()}", billing_cycle_day=1,
                           created_at=utcnow_naive() - datetime.timedelta(days=30))
        db.add(org)
        await db.flush()
        db.add(OrgSetting(org_id=org.id, key="share_tag_data", value="true"))
        await db.commit()
    _CREATED["orgs"].append(org.id)
    _CREATED["names"].append(name)
    return org.id, name


async def _opt_out(db: AsyncSession, org_id: int) -> None:
    setting = await db.scalar(select(OrgSetting).where(
        OrgSetting.org_id == org_id, OrgSetting.key == "share_tag_data"))
    setting.value = "false"
    await db.flush()
    await tag_service.withdraw_dictionary_contributions(db, org_id=org_id)


async def _state(f, org_id: int, name: str) -> tuple[int, int, tuple | None]:
    async with f() as db:
        tags = await db.scalar(select(func.count()).select_from(Tag).where(Tag.org_id == org_id))
        contributors = await db.scalar(
            select(func.count()).select_from(TagDictionaryContributor)
            .where(TagDictionaryContributor.contributor_org_id == org_id))
        counts = (await db.execute(
            select(TagDictionary.contributor_org_count, TagDictionary.usage_count)
            .where(TagDictionary.name_normalized == name))).first()
        return tags, contributors, tuple(counts) if counts else None


async def test_a_contribution_waits_for_an_opt_out_in_progress(factory):
    """Opt-out holds the row; the tag create waits, then sees sharing off.
    Wrong implementation: contributing on the first, unlocked read."""
    org_id, name = await _sharing_org(factory)
    async with factory() as opt_out, factory() as create:
        await _opt_out(opt_out, org_id)

        async def _create():
            await tag_service.create_tag(create, org_id=org_id, name=name,
                                         created_by_user_id=None)
            await create.commit()

        task = asyncio.create_task(_create())
        await asyncio.sleep(0.5)
        assert not task.done(), "the contribution did not wait for the opt-out's row lock"
        await opt_out.commit()
        await asyncio.wait_for(task, timeout=10)
    tags, contributors, _ = await _state(factory, org_id, name)
    assert (tags, contributors) == (1, 0)


async def test_an_opt_out_withdraws_a_contribution_that_committed_first(factory):
    """The tag create holds the row; the opt-out waits, then withdraws the
    contribution it could not see when it started. Wrong implementation: an
    unlocked withdrawal read, which misses the newly committed row."""
    org_id, name = await _sharing_org(factory)
    async with factory() as create, factory() as opt_out:
        await tag_service.create_tag(create, org_id=org_id, name=name, created_by_user_id=None)

        async def _opt_out_and_commit():
            await _opt_out(opt_out, org_id)
            await opt_out.commit()

        task = asyncio.create_task(_opt_out_and_commit())
        await asyncio.sleep(0.5)
        assert not task.done(), "the opt-out did not wait for the contribution's row lock"
        await create.commit()
        await asyncio.wait_for(task, timeout=10)
    tags, contributors, counts = await _state(factory, org_id, name)
    assert (tags, contributors, counts) == (1, 0, (0, 0))
