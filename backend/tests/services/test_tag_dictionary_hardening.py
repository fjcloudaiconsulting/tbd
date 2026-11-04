"""BE-18: counting in the shared tag dictionary.

1. An org counts once per tag: creating a tag again (or deleting and
   re-creating it) does not grow ``usage_count``.
2. Only contributors whose org is at least
   ``SHARED_DICTIONARY_MIN_ORG_AGE_DAYS`` old count towards the floor.
3. A contribution is withdrawn when the org deletes (or renames) the tag,
   and when it stops sharing (``withdraw_dictionary_contributions``; the
   settings routes are covered in ``tests/routers/test_tags_dictionary_hardening.py``).
4. Shared suggestions carry no cross-org count.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.settings import OrgSetting
from app.models.tag import TagDictionary, TagDictionaryContributor
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.tag import (
    SHARED_DICTIONARY_MIN_CONTRIBUTORS,
    SHARED_DICTIONARY_MIN_ORG_AGE_DAYS,
)
from app.services import admin_orgs_service, org_data_service, tag_service

AGED = utcnow_naive() - datetime.timedelta(days=SHARED_DICTIONARY_MIN_ORG_AGE_DAYS + 1)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _r):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _org(db, name: str, *, created_at=AGED, share: bool = True) -> int:
    org = Organization(name=name, billing_cycle_day=1, created_at=created_at)
    db.add(org)
    await db.flush()
    if share:
        db.add(OrgSetting(org_id=org.id, key="share_tag_data", value="true"))
    await db.commit()
    return org.id


async def _create(db, org_id: int, name: str):
    tag = await tag_service.create_tag(db, org_id=org_id, name=name, created_by_user_id=None)
    await db.commit()
    return tag


async def _transaction(db, org_id: int) -> int:
    at = AccountType(org_id=org_id, name="Checking", slug="checking", is_system=False)
    db.add(at)
    await db.flush()
    acc = Account(org_id=org_id, name="Main", account_type_id=at.id, currency="EUR",
                  balance=Decimal("0"), is_default=True)
    cat = Category(org_id=org_id, name="Bills", slug="bills", is_system=False,
                   type=CategoryType.EXPENSE)
    db.add_all([acc, cat])
    await db.flush()
    tx = Transaction(org_id=org_id, account_id=acc.id, category_id=cat.id, description="T",
                     amount=Decimal("1.00"),
                     type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                     date=datetime.date(2026, 5, 1), settled_date=datetime.date(2026, 5, 1))
    db.add(tx)
    await db.commit()
    return tx.id


async def _entry(db, name: str) -> TagDictionary | None:
    entry = await db.scalar(select(TagDictionary).where(TagDictionary.name_normalized == name))
    if entry is not None:
        await db.refresh(entry)
    return entry


async def _contributors(db, name: str) -> set[int]:
    rows = await db.execute(
        select(TagDictionaryContributor.contributor_org_id)
        .join(TagDictionary, TagDictionary.id == TagDictionaryContributor.dictionary_tag_id)
        .where(TagDictionary.name_normalized == name)
    )
    return set(rows.scalars().all())


async def _shared(db, org_id: int, prefix: str | None = None) -> list:
    res = await tag_service.suggest_tags(db, org_id=org_id, prefix=prefix, category_id=None,
                                         limit=25)
    return [s for s in res if s.source == "shared_dictionary"]


# ── 1. once per org ──────────────────────────────────────────────────────────


async def test_usage_counts_an_org_once(db):
    """Wrong implementation: ``usage_count += 1`` on every contribution."""
    org = await _org(db, "a")
    await _create(db, org, "insurance")
    await tag_service._record_dictionary_contribution(db, org_id=org, name_normalized="insurance")
    await db.commit()
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (1, 1)


async def test_delete_and_recreate_does_not_grow_usage(db):
    org = await _org(db, "a")
    for _ in range(3):
        tag = await _create(db, org, "insurance")
        await tag_service.delete_tag(db, org_id=org, tag_id=tag.id)
        await db.commit()
    await _create(db, org, "insurance")
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (1, 1)
    assert await _contributors(db, "insurance") == {org}


async def test_usage_counts_distinct_orgs(db):
    orgs = [await _org(db, n) for n in ("a", "b", "c")]
    for org in orgs:
        await _create(db, org, "insurance")
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (3, 3)


# ── 2. aged orgs only ────────────────────────────────────────────────────────


async def test_fresh_orgs_do_not_lift_a_tag_over_the_floor(db):
    """Wrong implementation: the floor read off ``contributor_org_count``,
    which a batch of brand-new orgs fills in minutes."""
    viewer = await _org(db, "viewer")
    now = utcnow_naive()
    for n in ("a", "b", "c"):
        await _create(db, await _org(db, n, created_at=now), "secret")
    assert (await _entry(db, "secret")).contributor_org_count == 3
    assert await _shared(db, viewer) == []


async def test_aged_orgs_reach_the_floor(db):
    viewer = await _org(db, "viewer")
    for n in ("a", "b", "c"):
        await _create(db, await _org(db, n), "insurance")
    assert [s.name for s in await _shared(db, viewer)] == ["insurance"]


async def test_floor_needs_k_aged_contributors(db):
    viewer = await _org(db, "viewer")
    young = utcnow_naive() - datetime.timedelta(days=SHARED_DICTIONARY_MIN_ORG_AGE_DAYS - 1)
    for n in ("a", "b"):
        await _create(db, await _org(db, n), "insurance")
    await _create(db, await _org(db, "c", created_at=young), "insurance")
    assert SHARED_DICTIONARY_MIN_CONTRIBUTORS == 3
    assert await _shared(db, viewer) == []

    # Once the third org is old enough, the tag surfaces with no new write.
    await db.execute(Organization.__table__.update()
                     .where(Organization.name == "c").values(created_at=AGED))
    await db.commit()
    assert [s.name for s in await _shared(db, viewer)] == ["insurance"]


async def test_seeds_still_bypass_the_floor(db):
    viewer = await _org(db, "viewer")
    db.add(TagDictionary(name_normalized="gym", contributor_org_count=0, usage_count=0,
                         is_seed=True))
    await db.commit()
    assert [s.name for s in await _shared(db, viewer)] == ["gym"]


# ── 3. withdrawal ────────────────────────────────────────────────────────────


async def test_delete_withdraws_only_this_orgs_contribution(db):
    """Wrong implementation: delete leaves the contributor row, so the tag
    stays counted for an org that no longer has it."""
    a, b = await _org(db, "a"), await _org(db, "b")
    tag_a = await _create(db, a, "insurance")
    await _create(db, b, "insurance")
    await tag_service.delete_tag(db, org_id=a, tag_id=tag_a.id)
    await db.commit()
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (1, 1)
    assert await _contributors(db, "insurance") == {b}


async def test_rename_withdraws_the_old_name(db):
    a, b = await _org(db, "a"), await _org(db, "b")
    tag_a = await _create(db, a, "insurance")
    await _create(db, b, "insurance")
    await tag_service.rename_tag(db, org_id=a, tag_id=tag_a.id, new_name="cover")
    await db.commit()
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (1, 1)
    assert await _contributors(db, "insurance") == {b}
    assert await _entry(db, "cover") is None  # a rename still contributes nothing


async def test_case_only_rename_keeps_the_contribution(db):
    a = await _org(db, "a")
    tag = await _create(db, a, "insurance")
    await tag_service.rename_tag(db, org_id=a, tag_id=tag.id, new_name="Insurance")
    await db.commit()
    assert await _contributors(db, "insurance") == {a}


async def test_withdraw_all_removes_every_contribution_of_the_org(db):
    a, b = await _org(db, "a"), await _org(db, "b")
    for name in ("insurance", "gym", "rent"):
        await _create(db, a, name)
    await _create(db, b, "rent")
    removed = await tag_service.withdraw_dictionary_contributions(db, org_id=a)
    await db.commit()
    assert removed == 3
    for name in ("insurance", "gym"):
        entry = await _entry(db, name)
        assert (entry.contributor_org_count, entry.usage_count) == (0, 0)
    rent = await _entry(db, "rent")
    assert (rent.contributor_org_count, rent.usage_count) == (1, 1)
    assert await db.scalar(
        select(func.count()).select_from(TagDictionaryContributor)
        .where(TagDictionaryContributor.contributor_org_id == a)
    ) == 0


async def test_withdraw_with_no_names_is_a_no_op(db):
    a = await _org(db, "a")
    await _create(db, a, "insurance")
    assert await tag_service.withdraw_dictionary_contributions(db, org_id=a, names=[]) == 0
    await db.commit()
    assert await _contributors(db, "insurance") == {a}


async def test_org_reset_and_delete_also_withdraw_usage(db):
    a, b, c = await _org(db, "a"), await _org(db, "b"), await _org(db, "c")
    for org in (a, b, c):
        await _create(db, org, "insurance")
    await org_data_service.wipe_org_data(db, org_id=a)
    await db.commit()
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (2, 2)
    await admin_orgs_service.delete_org_cascade(db, org_id=b)
    await db.commit()
    entry = await _entry(db, "insurance")
    assert (entry.contributor_org_count, entry.usage_count) == (1, 1)


# ── 4. no cross-org count in suggestions ─────────────────────────────────────


async def test_shared_suggestions_carry_no_weight(db):
    """Wrong implementation: ``weight=usage_count`` on the shared pass."""
    viewer = await _org(db, "viewer")
    for n in ("a", "b", "c"):
        await _create(db, await _org(db, n), "insurance")
    shared = await _shared(db, viewer)
    assert [(s.name, s.weight) for s in shared] == [("insurance", None)]


async def test_org_local_suggestions_keep_their_own_weight(db):
    viewer = await _org(db, "viewer")
    await _create(db, viewer, "gym")
    res = await tag_service.suggest_tags(db, org_id=viewer, prefix="g", category_id=None,
                                         limit=10)
    assert [(s.name, s.source, s.weight) for s in res] == [("gym", "org_recent", 0)]


@pytest.mark.parametrize("value", ["false", "True", ""])
def test_only_true_enables_sharing(value):
    assert tag_service.share_tag_data_value_enabled(value) is False
    assert tag_service.share_tag_data_value_enabled("true") is True


# ── 5. one budget of new tags per org, whatever creates them ─────────────────


async def test_tag_budget_counts_each_new_tag_per_org(db):
    """Wrong implementation: a limit on one route only, so another path
    creates tags without bound. ``create_tag`` is the one place tag rows are
    created, so the budget lives there."""
    a, b = await _org(db, "a", share=False), await _org(db, "b", share=False)
    for i in range(tag_service.TAG_CREATE_LIMIT):
        await _create(db, a, f"t{i}")
    with pytest.raises(tag_service.TagCreationLimited) as exc:
        await tag_service.create_tag(db, org_id=a, name="one-more", created_by_user_id=None)
    assert 1 <= exc.value.retry_after <= tag_service.TAG_CREATE_WINDOW_SECONDS
    await db.rollback()
    # An existing name is a conflict, not a new tag, and another org has its
    # own budget.
    with pytest.raises(tag_service.ConflictError):
        await tag_service.create_tag(db, org_id=a, name="t0", created_by_user_id=None)
    await _create(db, b, "t0")


async def test_tag_budget_also_counts_auto_created_tags(db):
    a = await _org(db, "a", share=False)
    for i in range(tag_service.TAG_CREATE_LIMIT):
        await _create(db, a, f"t{i}")
    tx = await _transaction(db, a)
    with pytest.raises(tag_service.TagCreationLimited):
        await tag_service.set_transaction_tags(db, org_id=a, transaction_id=tx,
                                               tag_names=["brand-new"], created_by_user_id=None)
    await db.rollback()
    # Re-using existing tags creates nothing and is not limited.
    await tag_service.set_transaction_tags(db, org_id=a, transaction_id=tx,
                                           tag_names=["t1", "t2"], created_by_user_id=None)


# ── 6. opt-out and contribution are exclusive ────────────────────────────────


async def test_contribution_rechecks_sharing_under_the_lock(db, monkeypatch):
    """The plain read said "sharing", but the row says otherwise by the time
    the contributor would be inserted (an opt-out committed in between): no
    contribution. Wrong implementation: trusting the first read."""
    a = await _org(db, "a", share=False)
    db.add(OrgSetting(org_id=a, key="share_tag_data", value="false"))
    await db.commit()

    async def _stale_read(_db, _org_id):
        return True

    monkeypatch.setattr(tag_service, "is_share_tag_data_enabled", _stale_read)
    await _create(db, a, "insurance")
    assert await _entry(db, "insurance") is None
    assert await _contributors(db, "insurance") == set()


async def test_sharing_recheck_and_withdrawal_use_locking_reads(db):
    """The two reads that make opt-out and contribution exclusive must be
    locking reads on MySQL (SQLite drops FOR UPDATE, so the rendered SQL is
    checked). The MySQL interleavings are in test_tag_dictionary_mysql.py."""
    from sqlalchemy.dialects import mysql

    a = await _org(db, "a")
    seen: list = []
    real = db.execute

    async def _spy(stmt, *args, **kwargs):
        seen.append(stmt)
        return await real(stmt, *args, **kwargs)

    db.execute = _spy  # type: ignore[assignment]
    try:
        await _create(db, a, "insurance")
        await tag_service.withdraw_dictionary_contributions(db, org_id=a)
    finally:
        db.execute = real  # type: ignore[assignment]
    rendered = [str(s.compile(dialect=mysql.dialect())) for s in seen if hasattr(s, "compile")]
    setting_reads = [r for r in rendered if "FROM org_settings" in r]
    contributor_reads = [r for r in rendered
                         if r.startswith("SELECT") and "FROM tag_dictionary_contributors" in r
                         and "contributor_org_id =" in r and "dictionary_tag_id =" not in r]
    assert any(r.rstrip().endswith("FOR UPDATE") for r in setting_reads), setting_reads
    assert contributor_reads and all(r.rstrip().endswith("FOR UPDATE") for r in contributor_reads)
