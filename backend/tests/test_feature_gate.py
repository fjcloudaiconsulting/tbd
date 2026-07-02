"""Feature gate resolution tests — three-level priority matrix.

Resolution order (lowest → highest priority):
  env-floor → global SystemSetting → per-org OrgSetting

Each level overrides the one below. Unrecognised / absent values fall
through to the next level.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.system_setting import SystemSetting
from app.models.settings import OrgSetting
from app.services.feature_gate import Feature, resolve_feature, feature_setting_key


# ---------------------------------------------------------------------------
# Isolated in-memory DB fixture (mirrors test_settled_invariant.py pattern)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_env_floor_when_no_rows(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "feature_reports_v2", True)
    assert await resolve_feature(Feature.REPORTS, org_id=1, db=db_session) is True
    monkeypatch.setattr(settings, "feature_reports_v2", False)
    assert await resolve_feature(Feature.REPORTS, org_id=1, db=db_session) is False


@pytest.mark.asyncio
async def test_global_overrides_floor(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "feature_plans", False)
    db_session.add(SystemSetting(key=feature_setting_key(Feature.PLANS), value="on"))
    await db_session.commit()
    assert await resolve_feature(Feature.PLANS, org_id=1, db=db_session) is True


@pytest.mark.asyncio
async def test_org_override_beats_global(db_session, monkeypatch):
    # Seed parent orgs so the org_settings FK is satisfied on SQLite.
    from app.models.user import Organization
    db_session.add(Organization(id=7, name="Org Seven", billing_cycle_day=1))
    db_session.add(Organization(id=8, name="Org Eight", billing_cycle_day=1))
    await db_session.flush()
    db_session.add(SystemSetting(key=feature_setting_key(Feature.REPORTS), value="off"))
    db_session.add(OrgSetting(org_id=7, key=feature_setting_key(Feature.REPORTS), value="on"))
    await db_session.commit()
    assert await resolve_feature(Feature.REPORTS, org_id=7, db=db_session) is True
    # other org still sees global off
    assert await resolve_feature(Feature.REPORTS, org_id=8, db=db_session) is False


@pytest.mark.asyncio
async def test_unrecognized_org_value_falls_through(db_session, monkeypatch):
    """An unrecognised org-level value must be ignored; resolution falls through
    to the next level (global SystemSetting) rather than treating it as off."""
    from app.models.user import Organization
    from app.config import settings
    monkeypatch.setattr(settings, "feature_plans", False)
    db_session.add(Organization(id=7, name="Org Seven", billing_cycle_day=1))
    await db_session.flush()
    # Global is "on"; org value is an unrecognised string ("maybe").
    db_session.add(SystemSetting(key=feature_setting_key(Feature.PLANS), value="on"))
    db_session.add(OrgSetting(org_id=7, key=feature_setting_key(Feature.PLANS), value="maybe"))
    await db_session.commit()
    # Unrecognised "maybe" at org level → falls through → global "on" → True.
    assert await resolve_feature(Feature.PLANS, org_id=7, db=db_session) is True


# ---------------------------------------------------------------------------
# CUSTOM_DASHBOARD gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_dashboard_defaults_on(db_session):
    """Feature.CUSTOM_DASHBOARD resolves ON by default now that the env-floor
    ships True (global flip) and no DB overrides exist."""
    from app.config import settings
    assert settings.feature_custom_dashboard is True
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=1, db=db_session) is True


@pytest.mark.asyncio
async def test_custom_dashboard_rolls_back_with_org_off_override(db_session, monkeypatch):
    """A per-org OrgSetting of 'off' rolls CUSTOM_DASHBOARD back for one org even
    when the env-floor ships True — the supported rollback that returns that org
    to LegacyDashboard. Other orgs stay ON."""
    from app.models.user import Organization
    from app.config import settings
    monkeypatch.setattr(settings, "feature_custom_dashboard", True)
    db_session.add(Organization(id=11, name="Org Eleven", billing_cycle_day=1))
    await db_session.flush()
    db_session.add(
        OrgSetting(org_id=11, key=feature_setting_key(Feature.CUSTOM_DASHBOARD), value="off")
    )
    await db_session.commit()
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=11, db=db_session) is False
    # Other org (no override) stays on via the env-floor.
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=999, db=db_session) is True


@pytest.mark.asyncio
async def test_custom_dashboard_flips_with_org_override(db_session, monkeypatch):
    """A per-org OrgSetting of 'on' turns CUSTOM_DASHBOARD on even when the
    env-floor is False — same three-level resolution as PLANS."""
    from app.models.user import Organization
    from app.config import settings
    monkeypatch.setattr(settings, "feature_custom_dashboard", False)
    db_session.add(Organization(id=10, name="Org Ten", billing_cycle_day=1))
    await db_session.flush()
    db_session.add(
        OrgSetting(org_id=10, key=feature_setting_key(Feature.CUSTOM_DASHBOARD), value="on")
    )
    await db_session.commit()
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=10, db=db_session) is True
    # Other org (no override) stays off.
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=999, db=db_session) is False


@pytest.mark.asyncio
async def test_custom_dashboard_flips_with_global_override(db_session, monkeypatch):
    """A global SystemSetting of 'on' turns CUSTOM_DASHBOARD on for all orgs."""
    from app.config import settings
    monkeypatch.setattr(settings, "feature_custom_dashboard", False)
    db_session.add(
        SystemSetting(key=feature_setting_key(Feature.CUSTOM_DASHBOARD), value="on")
    )
    await db_session.commit()
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=1, db=db_session) is True
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=2, db=db_session) is True


@pytest.mark.asyncio
async def test_custom_dashboard_env_floor_on(db_session, monkeypatch):
    """When env-floor is True (operator opted-in globally), gate resolves True."""
    from app.config import settings
    monkeypatch.setattr(settings, "feature_custom_dashboard", True)
    assert await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id=1, db=db_session) is True
