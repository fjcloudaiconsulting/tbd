"""user_merge_service unit tests.

Pins the FK-reassignment behavior for the
``POST /api/v1/admin/users/merge`` recovery endpoint. Each table
that carries a ``users.id`` FK gets a dedicated case to prove the
reassignment lands on the target before the source is deleted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.feature_override import OrgFeatureOverride
from app.models.invitation import Invitation
from app.models.org_data_reset_lock import OrgDataResetLock
from app.models.tag import Tag
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import user_merge_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_org(db: AsyncSession) -> Organization:
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    return org


async def _seed_user(
    db: AsyncSession,
    *,
    org_id: int,
    username: str,
    email: str,
    is_superadmin: bool = False,
    email_verified: bool = False,
    role: Role = Role.OWNER,
    is_active: bool = True,
) -> User:
    user = User(
        org_id=org_id,
        username=username,
        email=email,
        password_hash=hash_password("pw"),
        role=role,
        is_superadmin=is_superadmin,
        is_active=is_active,
        email_verified=email_verified,
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_merge_same_user_rejected(session_factory) -> None:
    async with session_factory() as db:
        org = await _seed_org(db)
        u = await _seed_user(db, org_id=org.id, username="a", email="a@x.io")
        await db.commit()
        with pytest.raises(ValidationError):
            await user_merge_service.merge_users(
                db, source_user_id=u.id, target_user_id=u.id
            )


@pytest.mark.asyncio
async def test_merge_missing_source(session_factory) -> None:
    async with session_factory() as db:
        org = await _seed_org(db)
        target = await _seed_user(db, org_id=org.id, username="t", email="t@x.io")
        await db.commit()
        with pytest.raises(NotFoundError):
            await user_merge_service.merge_users(
                db, source_user_id=9999, target_user_id=target.id
            )


@pytest.mark.asyncio
async def test_merge_missing_target(session_factory) -> None:
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(db, org_id=org.id, username="s", email="s@x.io")
        await db.commit()
        with pytest.raises(NotFoundError):
            await user_merge_service.merge_users(
                db, source_user_id=source.id, target_user_id=9999
            )


@pytest.mark.asyncio
async def test_merge_cross_org_rejected(session_factory) -> None:
    async with session_factory() as db:
        org_a = await _seed_org(db)
        org_b = Organization(name="Other", billing_cycle_day=1)
        db.add(org_b)
        await db.flush()
        source = await _seed_user(db, org_id=org_a.id, username="s", email="s@x.io")
        target = await _seed_user(db, org_id=org_b.id, username="t", email="t@x.io")
        await db.commit()
        with pytest.raises(ConflictError):
            await user_merge_service.merge_users(
                db, source_user_id=source.id, target_user_id=target.id
            )


@pytest.mark.asyncio
async def test_merge_superadmin_source_rejected(session_factory) -> None:
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(
            db, org_id=org.id, username="s", email="s@x.io", is_superadmin=True
        )
        target = await _seed_user(db, org_id=org.id, username="t", email="t@x.io")
        await db.commit()
        with pytest.raises(ConflictError):
            await user_merge_service.merge_users(
                db, source_user_id=source.id, target_user_id=target.id
            )


@pytest.mark.asyncio
async def test_merge_reassigns_audit_events_and_deletes_source(session_factory) -> None:
    """Audit rows survive the merge with attribution flipped to target."""
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(db, org_id=org.id, username="s", email="s@x.io")
        target = await _seed_user(db, org_id=org.id, username="t", email="t@x.io")
        for i in range(3):
            db.add(
                AuditEvent(
                    event_type=f"test.event.{i}",
                    actor_user_id=source.id,
                    actor_email="s@x.io",
                    target_org_id=org.id,
                    target_org_name="Acme",
                    outcome=AuditOutcome.SUCCESS,
                )
            )
        await db.commit()
        source_id, target_id = source.id, target.id

        counts = await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()

        assert counts["audit_events"] == 3
        remaining_source_events = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.actor_user_id == source_id)
            )
        ).scalars().all()
        assert remaining_source_events == []
        target_events = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.actor_user_id == target_id)
            )
        ).scalars().all()
        assert len(target_events) == 3
        # Snapshot email is unchanged — historical record.
        assert all(e.actor_email == "s@x.io" for e in target_events)
        # Source row is gone.
        assert (await db.scalar(select(User).where(User.id == source_id))) is None


@pytest.mark.asyncio
async def test_merge_reassigns_tags(session_factory) -> None:
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(db, org_id=org.id, username="s", email="s@x.io")
        target = await _seed_user(db, org_id=org.id, username="t", email="t@x.io")
        db.add(
            Tag(
                org_id=org.id,
                name="groceries",
                name_normalized="groceries",
                created_by_user_id=source.id,
            )
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        counts = await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()

        assert counts["tags"] == 1
        target_tags = (
            await db.execute(
                select(Tag).where(Tag.created_by_user_id == target_id)
            )
        ).scalars().all()
        assert len(target_tags) == 1
        assert target_tags[0].name == "groceries"


@pytest.mark.asyncio
async def test_merge_reassigns_invitations_and_overrides(session_factory) -> None:
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(db, org_id=org.id, username="s", email="s@x.io")
        target = await _seed_user(db, org_id=org.id, username="t", email="t@x.io")
        db.add(
            Invitation(
                org_id=org.id,
                email="new@x.io",
                role=Role.MEMBER,
                open_email="new@x.io",
                created_by=source.id,
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=7),
            )
        )
        db.add(
            OrgFeatureOverride(
                org_id=org.id,
                feature_key="some.feature",
                value=True,
                set_by=source.id,
            )
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        counts = await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()

        assert counts["invitations"] == 1
        assert counts["org_feature_overrides"] == 1


@pytest.mark.asyncio
async def test_merge_carries_email_verified_to_target(session_factory) -> None:
    """The SSO-row-merged-into-local-row scenario.

    The source row has ``email_verified=True`` (it came from Google,
    which we trust), the target row is the older local user which
    never verified. After the merge, target should be verified.
    """
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(
            db, org_id=org.id, username="s", email="s@x.io", email_verified=True
        )
        target = await _seed_user(
            db, org_id=org.id, username="t", email="t@x.io", email_verified=False
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()

        target_after = await db.scalar(select(User).where(User.id == target_id))
        assert target_after is not None
        assert target_after.email_verified is True


# ── last-active-owner invariant ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_blocked_when_source_is_sole_owner_and_target_in_different_org(
    session_factory,
) -> None:
    """Source is the only active owner of org X; target lives in org Y.
    Refuse with 409 — the merge would orphan org X.

    In practice the cross-org guard catches this scenario first
    (target.org_id != source.org_id), but the contract the operator
    sees is identical: ``ConflictError`` + source row preserved. This
    test pins both signals at once. If the cross-org guard is ever
    relaxed, the last-owner guard catches the same orphan case.
    """
    async with session_factory() as db:
        org_x = await _seed_org(db)
        org_y = Organization(name="Other", billing_cycle_day=1)
        db.add(org_y)
        await db.flush()
        source = await _seed_user(
            db, org_id=org_x.id, username="sole-x", email="sx@x.io",
            role=Role.OWNER, is_active=True,
        )
        target = await _seed_user(
            db, org_id=org_y.id, username="t-y", email="ty@x.io",
            role=Role.OWNER, is_active=True,
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        with pytest.raises(ConflictError):
            await user_merge_service.merge_users(
                db, source_user_id=source_id, target_user_id=target_id
            )

        # Source row preserved — guard fired before any FK reassignment.
        await db.rollback()
        async with session_factory() as fresh:
            kept = await fresh.scalar(select(User).where(User.id == source_id))
            assert kept is not None
            assert kept.is_active is True
            assert kept.role == Role.OWNER


@pytest.mark.asyncio
async def test_merge_blocked_when_source_is_sole_owner_and_target_is_same_org_member(
    session_factory,
) -> None:
    """Source is the only active owner of org X; target is a MEMBER of
    org X (not OWNER). Deleting source would still orphan the org —
    refuse with 409 with the last-active-owner error message.
    """
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(
            db, org_id=org.id, username="sole", email="s@x.io",
            role=Role.OWNER, is_active=True,
        )
        target = await _seed_user(
            db, org_id=org.id, username="member", email="m@x.io",
            role=Role.MEMBER, is_active=True,
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        with pytest.raises(ConflictError, match="only active owner"):
            await user_merge_service.merge_users(
                db, source_user_id=source_id, target_user_id=target_id
            )


@pytest.mark.asyncio
async def test_merge_blocked_when_target_is_inactive_owner_in_same_org(
    session_factory,
) -> None:
    """Same-org case where target IS an OWNER but is inactive.
    The invariant counts active owners only, so target cannot
    preserve it — block.
    """
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(
            db, org_id=org.id, username="sole-active", email="sa@x.io",
            role=Role.OWNER, is_active=True,
        )
        target = await _seed_user(
            db, org_id=org.id, username="inactive-owner", email="io@x.io",
            role=Role.OWNER, is_active=False,
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        with pytest.raises(ConflictError, match="only active owner"):
            await user_merge_service.merge_users(
                db, source_user_id=source_id, target_user_id=target_id
            )


@pytest.mark.asyncio
async def test_merge_allowed_when_source_is_one_of_multiple_active_owners(
    session_factory,
) -> None:
    """Org has two active owners; merging one into a member is fine —
    the other owner keeps the invariant.
    """
    async with session_factory() as db:
        org = await _seed_org(db)
        source = await _seed_user(
            db, org_id=org.id, username="owner-a", email="a@x.io",
            role=Role.OWNER, is_active=True,
        )
        _other_owner = await _seed_user(
            db, org_id=org.id, username="owner-b", email="b@x.io",
            role=Role.OWNER, is_active=True,
        )
        target = await _seed_user(
            db, org_id=org.id, username="member", email="m@x.io",
            role=Role.MEMBER, is_active=True,
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        # No raise — guard does not fire.
        await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()

        # Source row gone; org still has an active owner.
        assert (
            await db.scalar(select(User).where(User.id == source_id))
        ) is None
        active_owners = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.org_id == org.id,
                User.role == Role.OWNER,
                User.is_active.is_(True),
            )
        )
        assert (active_owners or 0) >= 1


@pytest.mark.asyncio
async def test_merge_allowed_when_source_is_not_owner_regardless_of_org_owner_count(
    session_factory,
) -> None:
    """Source is a MEMBER, not an owner. The owner-count invariant is
    irrelevant — merge proceeds.
    """
    async with session_factory() as db:
        org = await _seed_org(db)
        # Single active owner of the org (NOT the source).
        _sole_owner = await _seed_user(
            db, org_id=org.id, username="owner", email="o@x.io",
            role=Role.OWNER, is_active=True,
        )
        source = await _seed_user(
            db, org_id=org.id, username="src-member", email="sm@x.io",
            role=Role.MEMBER, is_active=True,
        )
        target = await _seed_user(
            db, org_id=org.id, username="tgt-member", email="tm@x.io",
            role=Role.MEMBER, is_active=True,
        )
        await db.commit()
        source_id, target_id = source.id, target.id

        await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()

        assert (
            await db.scalar(select(User).where(User.id == source_id))
        ) is None
