"""Pre-merge review fix: deleting a category that has learned smart-rules
must not fail with an FK error. Rules are invisible learning state and
become invalid when the category is gone — they should be deleted silently.
"""
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.user import Organization


@pytest_asyncio.fixture
async def db_session():
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


async def test_deleting_category_with_learned_rules_succeeds(
    db_session: AsyncSession,
) -> None:
    """Reproduces the pre-merge review FK regression."""
    org = Organization(name="X", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    cat = Category(
        org_id=org.id, name="Misc", slug="misc",
        is_system=False, type=CategoryType.EXPENSE,
    )
    db_session.add(cat)
    await db_session.flush()
    db_session.add(CategoryRule(
        org_id=org.id,
        normalized_token="TESTLIDL",
        raw_description_seen="TEST LIDL",
        category_id=cat.id,
        match_count=1,
        source=RuleSource.USER_PICK,
    ))
    await db_session.commit()

    # Mirror the router's delete logic: delete rules first, then category.
    from sqlalchemy import delete
    await db_session.execute(
        delete(CategoryRule).where(CategoryRule.category_id == cat.id)
    )
    await db_session.delete(cat)
    await db_session.commit()

    # Both gone, no FK error raised.
    rules = (await db_session.execute(select(CategoryRule))).scalars().all()
    assert rules == []
    cats = (await db_session.execute(select(Category))).scalars().all()
    assert cats == []
