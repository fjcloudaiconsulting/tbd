"""Service-layer tests for L3.8 — org member invitations and member
management. Pins the create / preview / accept / revoke / list / remove
flows independent of the HTTP router."""
from __future__ import annotations

import datetime
from app._time import utcnow_naive

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.invitation import Invitation
from app.models.user import Organization, Role, User
from app.security import create_invitation_token, hash_password, verify_password
from app.services import invitation_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError as SvcValidationError


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
    is_superadmin: bool = False,
) -> int:
    async with factory() as db:
        u = User(
            org_id=org_id,
            username=username,
            email=email,
            password_hash=hash_password("pw-1234567"),
            role=role,
            is_superadmin=is_superadmin,
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
        delta = inv.expires_at - utcnow_naive()
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
async def test_create_invitation_db_unique_loser_surfaces_as_conflict(session_factory):
    """Defense in depth — pre-check guards the happy path, the DB
    UNIQUE(org_id, open_email) catches the concurrent loser. Bypass
    the pre-check (simulating two requests that both passed it) and
    confirm the DB integrity error becomes a 409, not a 500."""
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="race@acme.io", role=Role.MEMBER,
        )
        await db.commit()

    async with session_factory() as db:
        real_execute = db.execute
        call_count = {"n": 0}

        class _NullResult:
            def scalar_one_or_none(self):
                return None

        async def fake_execute(stmt):
            call_count["n"] += 1
            # 3rd execute in create_invitation is the pending-row
            # lookup — bypass it so the duplicate insert flows to the
            # DB UNIQUE constraint.
            if call_count["n"] == 3:
                return _NullResult()
            return await real_execute(stmt)

        db.execute = fake_execute  # type: ignore[assignment]

        with pytest.raises(ConflictError, match="already invited"):
            await invitation_service.create_invitation(
                db, org_id=org_id, created_by=owner_id,
                email="race@acme.io", role=Role.MEMBER,
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
        b.revoked_at = utcnow_naive()
        c.open_email = None
        c.accepted_at = utcnow_naive()
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
        first.expires_at = utcnow_naive() - datetime.timedelta(days=1)
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
@pytest.mark.parametrize(
    ("invited_role", "prior_role"),
    [
        (Role.MEMBER, Role.ADMIN),
        (Role.ADMIN, Role.MEMBER),
    ],
    ids=["invited_as_member", "invited_as_admin"],
)
async def test_fence_reactivation_of_superadmin_is_refused(
    session_factory, invited_role, prior_role
):
    """FENCE (TBD-351). Kills BOTH wrong implementations of this branch:

      (a) reactivating a deprovisioned superadmin with the flag intact —
          ``has_permission`` short-circuits on ``is_superadmin`` BEFORE it
          consults the role, so the retained flag would beat the "member"
          the invitation granted;
      (b) *clearing* the flag here — which closes (a) and opens something
          worse. ``count(is_superadmin) >= 1`` is an inductive invariant on
          ``main``: the only three write sites are constructions
          (``auth.py:367``, ``auth.py:3397``, ``invitation_service.py:378``)
          and nothing ever sets an existing row's flag to False. A clear on
          this branch would be the first decrement primitive in the
          codebase, reachable from an UNAUTHENTICATED public route. Once
          the count hits 0 the register + Google bootstraps
          (``auth.py:350-353``, ``auth.py:3377-3380``) re-arm — they count
          the flag with NO ``is_active`` filter — and the Google callback
          mints a superadmin, verifies the email and issues a session in
          one uncaptcha'd redirect.

    The escalation assertion at the end is what pins (b) specifically: the
    platform superadmin count must be unchanged. Asserting only "refused"
    would go green against a clear that also happened to raise.

    The seed deliberately sets ``is_superadmin=True`` before the soft
    delete — without that this fence passes vacuously against a user that
    never held the flag.

    PARAMETRIZED over the invited role because ``role`` is first-class
    reachable input (``Literal["admin", "member"]`` at
    ``schemas/invitation.py:14``), and the guard is a two-cell space:
    (holds the flag) × (invited as member | admin). Covering one cell lets a
    role-conditional guard through — e.g.
    ``if existing.is_superadmin and inv.role == Role.MEMBER: raise`` — which
    would refuse member-invites while silently reactivating a superadmin
    invited as ADMIN, flag intact and no refusal. Same half-fix door as the
    original clear-based mutant, and worse here because the surviving path
    reactivates rather than merely retaining.

    ``prior_role`` is always the OTHER role from ``invited_role`` so the
    "role unchanged" assertion below stays non-trivial in both cells: with
    them equal it would hold even if the reactivation had gone through.
    """
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    assert prior_role != invited_role, "fixture must make the two roles differ"
    existing_id = await _add_user(
        session_factory, org_id=org_id, username="expo",
        email="expo@acme.io", role=prior_role, is_active=False,
        is_superadmin=True,
    )
    # Control: the flag really is on the row before the accept.
    async with session_factory() as db:
        before = (
            await db.execute(select(User).where(User.id == existing_id))
        ).scalar_one()
        assert before.is_superadmin is True
        original_hash = before.password_hash

    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="expo@acme.io", role=invited_role,
        )
        await db.commit()
        inv_id = inv.id
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        with pytest.raises(ConflictError, match="platform role"):
            await invitation_service.accept_invitation(
                db, token=token, username="expo", password="brand-new-pw-1234",
            )
        # Assert inside the SAME session, before it closes. The router's
        # ConflictError path never commits (``get_db`` only closes), so a
        # refusal that mutated the row first and raised second would be
        # silently discarded on rollback — and a post-commit assertion alone
        # would go green against it for the wrong reason. Measured: that
        # exact mutant passes without these two lines.
        in_session = (
            await db.execute(select(User).where(User.id == existing_id))
        ).scalar_one()
        assert in_session.is_superadmin is True, (
            "The refusal mutated is_superadmin before raising"
        )
        assert await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin == True)  # noqa: E712
        ) == 1

    async with session_factory() as db:
        after = (
            await db.execute(select(User).where(User.id == existing_id))
        ).scalar_one()
        # The refusal raises BEFORE any attribute assignment, so no field of
        # the user row moved — not even partially, via autoflush.
        assert after.is_superadmin is True
        assert after.is_active is False
        assert after.role == prior_role       # pre-removal role, not the invite's
        assert after.password_hash == original_hash
        assert after.password_changed_at is None
        assert after.sessions_invalidated_at is None

        # The invitation was NOT consumed — the org admin can still revoke it.
        refreshed_inv = (
            await db.execute(select(Invitation).where(Invitation.id == inv_id))
        ).scalar_one()
        assert refreshed_inv.accepted_at is None

        # THE ESCALATION ASSERTION. Nothing in this flow may decrement the
        # platform superadmin count; reaching 0 re-arms the first-user
        # bootstrap for the next arbitrary signup.
        assert await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin == True)  # noqa: E712
        ) == 1


@pytest.mark.asyncio
async def test_guard_reactivation_leaves_ordinary_member_intact(session_factory):
    """GUARD (TBD-351 control). An ordinary (never-superadmin) member
    round-trips through reactivation unaltered apart from the fields the
    flow is supposed to touch. Proves the superadmin clear is not a blunt
    instrument that damages the normal path."""
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    existing_id = await _add_user(
        session_factory, org_id=org_id, username="plain",
        email="plain@acme.io", role=Role.MEMBER, is_active=False,
    )
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="plain@acme.io", role=Role.ADMIN,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        await invitation_service.accept_invitation(
            db, token=token, username="ignored", password="brand-new-pw-1234",
        )
        await db.commit()

    async with session_factory() as db:
        after = (
            await db.execute(select(User).where(User.id == existing_id))
        ).scalar_one()
        assert after.id == existing_id
        assert after.username == "plain"      # username is not rewritten
        assert after.email == "plain@acme.io"
        assert after.is_active is True
        assert after.email_verified is True
        assert after.role == Role.ADMIN       # role comes from the invitation
        assert after.is_superadmin is False
        assert verify_password("brand-new-pw-1234", after.password_hash)


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
async def test_accept_reactivates_legacy_user_with_short_username(session_factory):
    """Username strict pattern (min_length=3) was added in PR #70 with
    a legacy-grandfathering rule: existing accounts keep shorter
    names. Reactivation must NOT re-validate the existing username
    against today's strict regex — the user can't change it via this
    flow anyway."""
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    existing_id = await _add_user(
        session_factory, org_id=org_id, username="ab",  # 2 chars — legacy
        email="legacy@acme.io", role=Role.MEMBER, is_active=False,
    )
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="legacy@acme.io", role=Role.ADMIN,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        user = await invitation_service.accept_invitation(
            db, token=token, username="ab", password="brand-new-pw-1234",
        )
        await db.commit()
        assert user.id == existing_id
        assert user.username == "ab"
        assert user.is_active is True


@pytest.mark.asyncio
async def test_accept_rejects_invalid_username_for_new_user(session_factory):
    """For new-user accepts only, the service enforces the strict
    username constraints from RegisterRequest (length + pattern)."""
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="bad@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    async with session_factory() as db:
        with pytest.raises(SvcValidationError):
            await invitation_service.accept_invitation(
                db, token=token, username="ab", password="strong-pw-1234",
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
        assert first_again.is_active is True  # sole remaining ACTIVE owner
        # ⚠ TBD-364 repair (F-10). This block previously passed an ADMIN actor
        # and asserted `match="owner"`. For an ADMIN actor the
        # ADMIN-cannot-remove-OWNER guard fires FIRST, and its message ("Only
        # an owner can remove another owner") ALSO matches that regex — so this
        # test had never once executed the last-active-OWNER guard it is named
        # after. Both halves are repaired: the actor is now an OWNER, and the
        # assertion is exact `code` equality rather than a regex two distinct
        # messages satisfy.
        #
        # The actor must be an INACTIVE owner: the active-owner COUNT counts
        # only ACTIVE owners, so an active-owner actor would be counted itself
        # and the guard could never fire. That is also precisely why this
        # branch is unreachable through the router (get_current_user rejects
        # inactive users) — see the F-3 fence.
        second_owner = (
            await db.execute(select(User).where(User.id == second_owner_id))
        ).scalar_one()
        assert second_owner.is_active is False  # removed above
        assert second_owner.role == Role.OWNER
        with pytest.raises(ConflictError) as excinfo:
            await invitation_service.remove_member(
                db,
                org_id=org_id,
                current_user=second_owner,
                target_user_id=owner_id,
            )
        assert excinfo.value.code == invitation_service.CODE_LAST_ACTIVE_OWNER


# ── TBD-364: remove_member superadmin guard ────────────────────────────────
#
# Spec: specs/2026-08-11-tbd-364-remove-member-superadmin-guard.md
#
# ⚠ Do NOT add a `count(is_superadmin) == 1` assertion to any fence below.
# Both bootstrap predicates count that flag with NO is_active filter
# (routers/auth.py:351, routers/auth.py:3407), so a soft delete leaves the
# count unchanged and such an assertion passes against the UNFIXED code.
# The load-bearing assertion is `is_active is True`, read INSIDE the session:
# the ConflictError path never commits, so a post-close read shows the
# pre-mutation row and is green even against a mutate-then-raise guard.


@pytest.mark.asyncio
@pytest.mark.parametrize("target_role", [Role.MEMBER, Role.ADMIN, Role.OWNER])
async def test_remove_member_refuses_platform_superadmin(session_factory, target_role):
    """F-1. An org ADMIN cannot remove a platform superadmin, and the row is
    left completely untouched.

    Kills: the shipped defect; a guard that mutates before raising; a
    role-conditional guard (`... and target.role == Role.MEMBER`); a subject
    swap (`if current_user.is_superadmin`) — the actor here is not one.

    ⚠ Role.OWNER is the param that earns its keep. MEMBER and ADMIN traverse
    byte-identical code, because every guard after the superadmin check
    branches on `target.role == Role.OWNER`. Only the OWNER param exercises
    the ordering ruling that the superadmin guard precedes BOTH owner guards
    — and an org ADMIN removing a superadmin who is also an org OWNER is the
    most operationally likely shape of this bug. Without it, a guard placed
    after the ADMIN-cannot-remove-OWNER check would report
    `owner_removal_requires_owner` (and audit the wrong reason) with the whole
    suite green.
    """
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    admin_id = await _add_user(
        session_factory, org_id=org_id, username="orgadmin",
        email="orgadmin@acme.io", role=Role.ADMIN,
    )
    target_id = await _add_user(
        session_factory, org_id=org_id, username="platsa",
        email="platsa@acme.io", role=target_role, is_superadmin=True,
    )
    async with session_factory() as db:
        admin = (
            await db.execute(select(User).where(User.id == admin_id))
        ).scalar_one()
        with pytest.raises(ConflictError) as excinfo:
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=admin, target_user_id=target_id,
            )
        assert excinfo.value.code == invitation_service.CODE_TARGET_IS_SUPERADMIN

        # Read INSIDE the session — see the module note above.
        target = (
            await db.execute(select(User).where(User.id == target_id))
        ).scalar_one()
        assert target.is_active is True
        assert target.sessions_invalidated_at is None


@pytest.mark.asyncio
async def test_remove_member_refuses_already_inactive_superadmin(session_factory):
    """F-2. THE fence for the guard-placement ruling.

    Kills: the guard placed AFTER the `if not target.is_active: return target`
    early-return. That mutant is green on every other test in the suite —
    measured. (is_superadmin=True, is_active=False) is exactly the state the
    unguarded endpoint produced, so answering 204 there would confirm the
    lockout instead of reporting it.
    """
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    admin_id = await _add_user(
        session_factory, org_id=org_id, username="orgadmin2",
        email="orgadmin2@acme.io", role=Role.ADMIN,
    )
    target_id = await _add_user(
        session_factory, org_id=org_id, username="platsa2",
        email="platsa2@acme.io", role=Role.MEMBER,
        is_superadmin=True, is_active=False,
    )
    async with session_factory() as db:
        admin = (
            await db.execute(select(User).where(User.id == admin_id))
        ).scalar_one()
        with pytest.raises(ConflictError) as excinfo:
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=admin, target_user_id=target_id,
            )
        assert excinfo.value.code == invitation_service.CODE_TARGET_IS_SUPERADMIN


@pytest.mark.asyncio
async def test_remove_member_superadmin_reason_wins_over_last_owner(session_factory):
    """F-3. Ordering: the superadmin guard precedes both OWNER guards, so the
    strongest protection is the one reported.

    Service-level by necessity: the last-owner branch is UNREACHABLE through
    the router. It needs an OWNER actor, and `get_current_user` rejects
    inactive users (deps.py:56), so via HTTP the actor is an active OWNER of
    the org and is counted alongside the target — active_owners >= 2, always.
    An inactive-OWNER actor is constructible only below HTTP.

    Kills: the guard appended at the END of the guard block, which would
    report `last_active_owner` instead.
    """
    org_id, owner_id = await _seed_org_with_owner(session_factory)
    # Sole ACTIVE owner is the superadmin target; the actor is an INACTIVE
    # owner so it is excluded from the active-owner count.
    async with session_factory() as db:
        seeded_owner = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        seeded_owner.is_active = False
        await db.commit()
    target_id = await _add_user(
        session_factory, org_id=org_id, username="saowner",
        email="saowner@acme.io", role=Role.OWNER, is_superadmin=True,
    )
    async with session_factory() as db:
        actor = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        with pytest.raises(ConflictError) as excinfo:
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=actor, target_user_id=target_id,
            )
        assert excinfo.value.code == invitation_service.CODE_TARGET_IS_SUPERADMIN

    # CONTROL: identical fixture with the superadmin flag cleared must now
    # fall through to the last-owner guard. Without this, the fence above
    # would pass even if the fixture never reached the last-owner branch.
    async with session_factory() as db:
        target = (
            await db.execute(select(User).where(User.id == target_id))
        ).scalar_one()
        target.is_superadmin = False
        await db.commit()
    async with session_factory() as db:
        actor = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one()
        with pytest.raises(ConflictError) as excinfo:
            await invitation_service.remove_member(
                db, org_id=org_id, current_user=actor, target_user_id=target_id,
            )
        assert excinfo.value.code == invitation_service.CODE_LAST_ACTIVE_OWNER
