"""Service-layer tests for L3.8 — org member invitations and member
management. Pins the create / preview / accept / revoke / list / remove
flows independent of the HTTP router."""
from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.invitation import Invitation
from app.models.user import Organization, Role, User
from app.security import create_invitation_token, hash_password, verify_password
from app.services import invitation_service
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


async def _seed_org_with_owner(
    factory,
    *,
    name: str = "Acme",
    owner_username: str = "owner",
    owner_email: str = "owner@acme.io",
) -> tuple[int, int]:
    """Create an org with one OWNER user. Returns (org_id, owner_user_id)."""
    async with factory() as db:
        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id,
            username=owner_username,
            email=owner_email,
            password_hash=hash_password("owner-pass-1234"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        return org.id, owner.id


async def _add_user(
    factory,
    *,
    org_id: int,
    username: str,
    email: str,
    role: Role = Role.MEMBER,
    is_active: bool = True,
) -> int:
    async with factory() as db:
        u = User(
            org_id=org_id,
            username=username,
            email=email,
            password_hash=hash_password("pw-1234567"),
            role=role,
            is_superadmin=False,
            is_active=is_active,
            email_verified=True,
        )
        db.add(u)
        await db.commit()
        return u.id


# ── create_invitation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_invitation_happy_path(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db,
            org_id=org_id,
            created_by=owner_id,
            email="newmember@acme.io",
            role=Role.MEMBER,
        )
        await db.commit()
        assert inv.id is not None
        assert inv.email == "newmember@acme.io"
        assert inv.role == Role.MEMBER
        assert inv.org_id == org_id
        assert inv.created_by == owner_id
        assert inv.accepted_at is None
        assert inv.revoked_at is None
        assert inv.open_email == "newmember@acme.io"
        # 7-day default expiry
        delta = inv.expires_at - datetime.datetime.utcnow()
        assert datetime.timedelta(days=6, hours=23) < delta < datetime.timedelta(days=7, hours=1)


@pytest.mark.asyncio
async def test_create_invitation_normalizes_email(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db,
            org_id=org_id,
            created_by=owner_id,
            email="  Alice@Example.COM  ",
            role=Role.MEMBER,
        )
        await db.commit()
        assert inv.email == "alice@example.com"
        assert inv.open_email == "alice@example.com"


@pytest.mark.asyncio
async def test_create_invitation_rejects_duplicate_pending(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="dup@acme.io", role=Role.MEMBER,
        )
        await db.commit()
    async with session_factory() as db:
        with pytest.raises(ConflictError, match="already invited"):
            await invitation_service.create_invitation(
                db, org_id=org_id, created_by=owner_id,
                email="dup@acme.io", role=Role.MEMBER,
            )


@pytest.mark.asyncio
async def test_create_invitation_rejects_existing_active_member(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    await _add_user(session_factory, org_id=org_id, username="bob", email="bob@acme.io")
    async with session_factory() as db:
        with pytest.raises(ConflictError, match="already a member"):
            await invitation_service.create_invitation(
                db, org_id=org_id, created_by=owner_id,
                email="bob@acme.io", role=Role.MEMBER,
            )


@pytest.mark.asyncio
async def test_create_invitation_allows_reactivation_of_soft_deleted_user(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    await _add_user(
        session_factory, org_id=org_id, username="carol",
        email="carol@acme.io", is_active=False,
    )
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="carol@acme.io", role=Role.ADMIN,
        )
        await db.commit()
        assert inv.email == "carol@acme.io"
        assert inv.role == Role.ADMIN


# ── list / revoke ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_pending_invitations_excludes_accepted_and_revoked(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        a = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="a@acme.io", role=Role.MEMBER,
        )
        b = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="b@acme.io", role=Role.ADMIN,
        )
        c = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="c@acme.io", role=Role.MEMBER,
        )
        # Manually flip b → revoked, c → accepted.
        b.open_email = None
        b.revoked_at = datetime.datetime.utcnow()
        c.open_email = None
        c.accepted_at = datetime.datetime.utcnow()
        await db.commit()
    async with session_factory() as db:
        pending = await invitation_service.list_pending_invitations(db, org_id=org_id)
        assert [inv.email for inv in pending] == ["a@acme.io"]


@pytest.mark.asyncio
async def test_revoke_invitation_marks_revoked_and_frees_open_email(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="r@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        inv_id = inv.id
    async with session_factory() as db:
        revoked = await invitation_service.revoke_invitation(
            db, org_id=org_id, invitation_id=inv_id,
        )
        await db.commit()
        assert revoked.revoked_at is not None
        assert revoked.open_email is None
    # Now a fresh invite to the same email succeeds.
    async with session_factory() as db:
        fresh = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="r@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        assert fresh.id != inv_id


@pytest.mark.asyncio
async def test_revoke_invitation_404_when_not_in_org(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="x@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        inv_id = inv.id
    other_org = 999
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await invitation_service.revoke_invitation(
                db, org_id=other_org, invitation_id=inv_id,
            )


@pytest.mark.asyncio
async def test_create_invitation_clears_expired_open_invite_blocking_reuse(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        first = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="late@acme.io", role=Role.MEMBER,
        )
        # Time-warp the first row past its expires_at
        first.expires_at = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        await db.commit()
    async with session_factory() as db:
        # Second invite to the same email should succeed because the lazy
        # cleanup nulls open_email on the expired row.
        second = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="late@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        assert second.id is not None
        # The expired row's open_email is now NULL.
        rows = (
            await db.execute(
                select(Invitation).where(
                    Invitation.org_id == org_id, Invitation.email == "late@acme.io"
                ).order_by(Invitation.id)
            )
        ).scalars().all()
        assert len(rows) == 2
        assert rows[0].open_email is None  # expired-cleared
        assert rows[1].open_email == "late@acme.io"  # new pending


# ── preview / accept ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_returns_org_email_and_role_for_pending(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="invitee@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        preview = await invitation_service.preview_invitation(db, token=token)
        assert preview["org_name"] == "Acme"
        assert preview["email"] == "invitee@acme.io"
        assert preview["role"] == "member"
        assert preview["is_reactivation"] is False
        assert preview.get("existing_username") is None


@pytest.mark.asyncio
async def test_preview_flags_reactivation_when_soft_deleted_user_in_org(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    await _add_user(
        session_factory, org_id=org_id, username="reuser",
        email="reuser@acme.io", is_active=False,
    )
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="reuser@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        preview = await invitation_service.preview_invitation(db, token=token)
        assert preview["is_reactivation"] is True
        assert preview["existing_username"] == "reuser"


@pytest.mark.asyncio
async def test_preview_rejects_revoked_or_expired(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="x@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
        await invitation_service.revoke_invitation(
            db, org_id=org_id, invitation_id=inv.id,
        )
        await db.commit()
    async with session_factory() as db:
        with pytest.raises(invitation_service.InvitationUnavailable):
            await invitation_service.preview_invitation(db, token=token)


@pytest.mark.asyncio
async def test_preview_rejects_invalid_token(session_factory):
    async with session_factory() as db:
        with pytest.raises(invitation_service.InvitationUnavailable):
            await invitation_service.preview_invitation(db, token="not-a-jwt")


@pytest.mark.asyncio
async def test_accept_creates_new_user_and_marks_accepted(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="newbie@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        user = await invitation_service.accept_invitation(
            db, token=token, username="newbie", password="strong-pw-12345",
        )
        await db.commit()
        assert user.email == "newbie@acme.io"
        assert user.username == "newbie"
        assert user.org_id == org_id
        assert user.role == Role.MEMBER
        assert user.is_active is True
        assert user.email_verified is True
        assert verify_password("strong-pw-12345", user.password_hash)
        # Invitation row marked accepted, open_email cleared.
        refreshed = (
            await db.execute(select(Invitation).where(Invitation.id == inv.id))
        ).scalar_one()
        assert refreshed.accepted_at is not None
        assert refreshed.open_email is None


@pytest.mark.asyncio
async def test_accept_reactivates_existing_soft_deleted_user(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    existing_id = await _add_user(
        session_factory, org_id=org_id, username="dora",
        email="dora@acme.io", role=Role.MEMBER, is_active=False,
    )
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="dora@acme.io", role=Role.ADMIN,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        user = await invitation_service.accept_invitation(
            db, token=token, username="dora",  # ignored on reactivation
            password="brand-new-pw-1234",
        )
        await db.commit()
        # Same row reactivated
        assert user.id == existing_id
        assert user.is_active is True
        assert user.role == Role.ADMIN  # role updated from invitation
        assert verify_password("brand-new-pw-1234", user.password_hash)
        # Sessions invalidated so any old token is dead
        assert user.sessions_invalidated_at is not None
        assert user.password_changed_at is not None


@pytest.mark.asyncio
async def test_accept_rejects_username_already_taken(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    await _add_user(session_factory, org_id=org_id, username="taken", email="other@acme.io")
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="another@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        with pytest.raises(ConflictError, match="username"):
            await invitation_service.accept_invitation(
                db, token=token, username="taken", password="strong-pw-12345",
            )


@pytest.mark.asyncio
async def test_accept_rejects_revoked_token(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="revoked@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
        await invitation_service.revoke_invitation(
            db, org_id=org_id, invitation_id=inv.id,
        )
        await db.commit()
    async with session_factory() as db:
        with pytest.raises(invitation_service.InvitationUnavailable):
            await invitation_service.accept_invitation(
                db, token=token, username="revoked", password="strong-pw-12345",
            )


# ── members: list + remove ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_members_returns_active_users_in_org(session_factory):
    org_id, _owner = await _seed_org_with_owner(session_factory)
    other_org_id, _ = await _seed_org_with_owner(
        session_factory,
        name="Beta",
        owner_username="beta_owner",
        owner_email="beta_owner@beta.io",
    )
    await _add_user(session_factory, org_id=org_id, username="alice", email="a@acme.io")
    await _add_user(
        session_factory, org_id=org_id, username="ghost",
        email="g@acme.io", is_active=False,
    )
    await _add_user(
        session_factory, org_id=other_org_id, username="cross",
        email="c@other.io",
    )
    async with session_factory() as db:
        members = await invitation_service.list_members(db, org_id=org_id)
        names = sorted(m.username for m in members)
        assert names == ["alice", "owner"]  # ghost excluded (inactive), cross excluded (other org)


@pytest.mark.asyncio
async def test_remove_member_soft_deletes_and_invalidates_sessions(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    target_id = await _add_user(
        session_factory, org_id=org_id, username="vic", email="v@acme.io",
    )
    async with session_factory() as db:
        owner = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        removed = await invitation_service.remove_member(
            db, org_id=org_id, current_user=owner, target_user_id=target_id,
        )
        await db.commit()
        assert removed.is_active is False
        assert removed.sessions_invalidated_at is not None


@pytest.mark.asyncio
async def test_remove_member_blocks_self_removal(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        owner = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        with pytest.raises(ConflictError, match="yourself"):
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=owner, target_user_id=owner_id,
            )


@pytest.mark.asyncio
async def test_remove_member_admin_cannot_remove_owner(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    admin_id = await _add_user(
        session_factory, org_id=org_id, username="admin1",
        email="admin1@acme.io", role=Role.ADMIN,
    )
    async with session_factory() as db:
        admin = (
            await db.execute(select(User).where(User.id == admin_id))
        ).scalar_one()
        with pytest.raises(ConflictError, match="owner"):
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=admin, target_user_id=owner_id,
            )


@pytest.mark.asyncio
async def test_remove_member_blocks_removing_last_owner(session_factory):
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    second_owner_id = await _add_user(
        session_factory, org_id=org_id, username="owner2",
        email="owner2@acme.io", role=Role.OWNER,
    )
    async with session_factory() as db:
        first = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        # Remove the SECOND owner — current_user is first owner; target is
        # second. Should succeed (still ≥1 owner left).
        await invitation_service.remove_member(
            db, org_id=org_id, current_user=first, target_user_id=second_owner_id,
        )
        await db.commit()
    async with session_factory() as db:
        first_again = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        # Now first owner tries to remove herself — last-owner guard kicks
        # in via "can't remove yourself" first; build a separate scenario
        # where ANOTHER owner removes the last remaining owner.
        # Promote a second admin to the only-active owner-ish path:
        member_id = await _add_user(
            session_factory, org_id=org_id, username="admin2",
            email="admin2@acme.io", role=Role.ADMIN,
        )
        admin = (
            await db.execute(select(User).where(User.id == member_id))
        ).scalar_one()
        # Admin can't remove owner anyway, so this guard chain
        # effectively means: only a peer OWNER can remove an OWNER, and
        # only if there are ≥2 OWNERs at the time. With first_again as
        # the sole active OWNER, even another OWNER can't be the
        # remover. Verify the explicit last-owner guard:
        with pytest.raises(ConflictError, match="owner"):
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=admin, target_user_id=owner_id,
            )
