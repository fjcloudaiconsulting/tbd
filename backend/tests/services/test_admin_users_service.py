"""Service-layer coverage for ``admin_users_service.delete_user``.

Pinned behaviors:

- Happy path: deactivated user with attributable FK rows is deleted;
  invitations and import_batches keyed to them are removed; SET NULL
  FKs (audit_events.actor_user_id, tags.created_by_user_id, etc.)
  null out via the cascade.
- Preconditions: self-target, superadmin target, still-active target
  all raise ``ConflictError`` with the stable ``code`` so the router
  can return a structured payload.
- Not-found: deleting a non-existent user raises ``NotFoundError``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.invitation import Invitation
from app.models.tag import Tag
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import admin_users_service
from app.services.admin_users_service import (
    CODE_USER_IS_SELF,
    CODE_USER_IS_SUPERADMIN,
    CODE_USER_STILL_ACTIVE,
)
from app.services.exceptions import ConflictError, NotFoundError


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


async def _seed(factory) -> dict:
    """One org. An active superadmin actor, an inactive deletable user,
    an active user, and a superadmin user living in the same org.
    """
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        actor = User(
            org_id=org.id, username="root", email="root@platform.io",
            password_hash=hash_password("pw"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        inactive = User(
            org_id=org.id, username="ghost", email="ghost@acme.io",
            password_hash=hash_password("pw"),
            role=Role.MEMBER, is_active=False, email_verified=True,
        )
        active = User(
            org_id=org.id, username="alice", email="alice@acme.io",
            password_hash=hash_password("pw"),
            role=Role.ADMIN, is_active=True, email_verified=True,
        )
        embedded_sa = User(
            org_id=org.id, username="sa2", email="sa2@acme.io",
            password_hash=hash_password("pw"),
            role=Role.ADMIN, is_superadmin=True, is_active=False,
            email_verified=True,
        )
        db.add_all([actor, inactive, active, embedded_sa])
        await db.commit()

        return {
            "org_id": org.id,
            "actor_id": actor.id,
            "inactive_id": inactive.id,
            "active_id": active.id,
            "embedded_sa_id": embedded_sa.id,
        }


@pytest.mark.asyncio
async def test_delete_user_happy_path(session_factory):
    seed = await _seed(session_factory)

    # Plant FK references that should be cleaned up vs left alone.
    # ImportBatch is exercised in the router test against MySQL where
    # the Account scaffolding is already in place via the seed helpers;
    # here we stick to FKs that don't require an Account row so the
    # SQLite-in-memory fixture stays lean.
    async with session_factory() as db:
        import datetime as _dt

        # invitations.created_by — must be deleted before the user.
        inv = Invitation(
            org_id=seed["org_id"],
            email="invitee@acme.io",
            role=Role.MEMBER,
            created_by=seed["inactive_id"],
            expires_at=_dt.datetime(2030, 1, 1),
        )
        # audit_events.actor_user_id — SET NULL on delete (cascade).
        ev = AuditEvent(
            event_type="anything",
            actor_user_id=seed["inactive_id"],
            actor_email="ghost@acme.io",
            target_org_id=seed["org_id"],
            target_org_name="Acme",
            outcome=AuditOutcome.SUCCESS,
        )
        # tags.created_by_user_id — SET NULL on delete (cascade).
        tag = Tag(
            org_id=seed["org_id"],
            name="x",
            name_normalized="x",
            created_by_user_id=seed["inactive_id"],
        )
        db.add_all([inv, ev, tag])
        await db.commit()
        tag_id = tag.id
        ev_id = ev.id

    async with session_factory() as db:
        result = await admin_users_service.delete_user(
            db,
            target_user_id=seed["inactive_id"],
            actor_user_id=seed["actor_id"],
        )
        await db.commit()

    assert result["snapshot"]["id"] == seed["inactive_id"]
    assert result["fk_cleanup_counts"]["invitations"] == 1
    # No ImportBatch planted in this case; count must be zero.
    assert result["fk_cleanup_counts"]["import_batches"] == 0

    async with session_factory() as db:
        gone = await db.get(User, seed["inactive_id"])
        assert gone is None
        # Invitations keyed to the user are deleted.
        invs = (await db.execute(select(Invitation))).scalars().all()
        assert invs == []
        # SET NULL FKs: audit_event row survives, actor_user_id is None.
        ev_after = await db.get(AuditEvent, ev_id)
        assert ev_after is not None
        assert ev_after.actor_user_id is None
        # tags.created_by_user_id nulls out.
        tag_after = await db.get(Tag, tag_id)
        assert tag_after is not None
        assert tag_after.created_by_user_id is None


@pytest.mark.asyncio
async def test_delete_user_refuses_self_target(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        with pytest.raises(ConflictError) as excinfo:
            await admin_users_service.delete_user(
                db,
                target_user_id=seed["actor_id"],
                actor_user_id=seed["actor_id"],
            )
    assert excinfo.value.code == CODE_USER_IS_SELF


@pytest.mark.asyncio
async def test_delete_user_refuses_active_target(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        with pytest.raises(ConflictError) as excinfo:
            await admin_users_service.delete_user(
                db,
                target_user_id=seed["active_id"],
                actor_user_id=seed["actor_id"],
            )
    assert excinfo.value.code == CODE_USER_STILL_ACTIVE


@pytest.mark.asyncio
async def test_delete_user_refuses_superadmin_target(session_factory):
    seed = await _seed(session_factory)
    # embedded_sa is inactive but a superadmin — the superadmin guard
    # must trip before the active guard, otherwise an inactive
    # superadmin would be deletable.
    async with session_factory() as db:
        with pytest.raises(ConflictError) as excinfo:
            await admin_users_service.delete_user(
                db,
                target_user_id=seed["embedded_sa_id"],
                actor_user_id=seed["actor_id"],
            )
    assert excinfo.value.code == CODE_USER_IS_SUPERADMIN


@pytest.mark.asyncio
async def test_delete_user_not_found(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await admin_users_service.delete_user(
                db,
                target_user_id=99999,
                actor_user_id=seed["actor_id"],
            )
