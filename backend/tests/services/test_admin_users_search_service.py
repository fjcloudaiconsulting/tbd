"""Service-layer coverage for ``admin_users_search_service`` (L4.4 slice).

Pinned behaviors:

- Cross-org search returns users from every org, not the actor's only.
- ``q`` matches email/username prefix and display-name substring,
  case-insensitively.
- LIKE metacharacters in ``q`` are escaped so a raw ``%`` does not
  widen the match.
- ``org_filter`` / ``role_filter`` / ``status_filter`` narrow correctly.
- ``get_user_detail`` returns the user's single org membership plus
  the recent audit events authored by them.
- Detail-not-found raises ``NotFoundError``.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import admin_users_search_service
from app.services.exceptions import NotFoundError


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed(factory) -> dict:
    """Two orgs, four users across them, with varied flags."""
    async with factory() as db:
        org_a = Organization(name="Acme", billing_cycle_day=1)
        org_b = Organization(name="Beta", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        alice = User(
            org_id=org_a.id, username="alice", email="alice@acme.io",
            first_name="Alice", last_name="Smith",
            password_hash=hash_password("pw"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True, mfa_enabled=True,
        )
        bob = User(
            org_id=org_a.id, username="bob", email="bob@acme.io",
            first_name="Bob",
            password_hash=hash_password("pw"),
            role=Role.ADMIN, is_active=True, email_verified=False,
        )
        carol = User(
            org_id=org_b.id, username="carol", email="carol@beta.io",
            first_name="Carol", last_name="Jones",
            password_hash=hash_password("pw"),
            role=Role.MEMBER, is_active=False, email_verified=True,
        )
        dave = User(
            org_id=org_b.id, username="dave_admin", email="dave@beta.io",
            password_hash=hash_password("pw"),
            role=Role.OWNER, is_active=True, email_verified=True,
        )
        db.add_all([alice, bob, carol, dave])
        await db.commit()

        # Two audit events authored by Alice. recent_audit_events
        # should return them in DESC order.
        e1 = AuditEvent(
            event_type="admin.org.delete",
            actor_user_id=alice.id,
            actor_email=alice.email,
            target_org_id=org_b.id,
            target_org_name=org_b.name,
            outcome=AuditOutcome.SUCCESS,
            detail={"snapshot": {"name": "Beta"}},
        )
        e2 = AuditEvent(
            event_type="admin.org.subscription.override",
            actor_user_id=alice.id,
            actor_email=alice.email,
            target_org_id=org_a.id,
            target_org_name=org_a.name,
            outcome=AuditOutcome.SUCCESS,
            detail={},
        )
        db.add_all([e1, e2])
        await db.commit()

        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "alice_id": alice.id,
            "bob_id": bob.id,
            "carol_id": carol.id,
            "dave_id": dave.id,
        }


# ── list_users ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_returns_cross_org(session_factory) -> None:
    """No filter: all users across both orgs come back."""
    ids = await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db)
    assert result["total"] == 4
    emails = {item["email"] for item in result["items"]}
    assert emails == {
        "alice@acme.io", "bob@acme.io",
        "carol@beta.io", "dave@beta.io",
    }


@pytest.mark.asyncio
async def test_list_users_q_matches_email_prefix(session_factory) -> None:
    """``q='ali'`` matches alice via email prefix."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, q="ali")
    emails = {item["email"] for item in result["items"]}
    assert emails == {"alice@acme.io"}


@pytest.mark.asyncio
async def test_list_users_q_matches_username_prefix(session_factory) -> None:
    """``q='dave'`` matches dave_admin via username prefix."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, q="dave")
    emails = {item["email"] for item in result["items"]}
    assert emails == {"dave@beta.io"}


@pytest.mark.asyncio
async def test_list_users_q_matches_name_substring(session_factory) -> None:
    """``q='jones'`` matches Carol Jones via display-name substring."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, q="jones")
    emails = {item["email"] for item in result["items"]}
    assert emails == {"carol@beta.io"}


@pytest.mark.asyncio
async def test_list_users_q_is_case_insensitive(session_factory) -> None:
    """Upper-case query matches lower-case email."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, q="ALICE")
    assert result["total"] == 1
    assert result["items"][0]["email"] == "alice@acme.io"


@pytest.mark.asyncio
async def test_list_users_q_escapes_like_metacharacters(session_factory) -> None:
    """``q='%'`` must NOT match every user (LIKE-wildcard escape)."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, q="%")
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_list_users_org_filter(session_factory) -> None:
    """``org_filter`` narrows to a single org."""
    ids = await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(
            db, org_filter=ids["org_b_id"]
        )
    emails = {item["email"] for item in result["items"]}
    assert emails == {"carol@beta.io", "dave@beta.io"}


@pytest.mark.asyncio
async def test_list_users_role_filter(session_factory) -> None:
    """``role_filter='member'`` returns only members."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, role_filter="member")
    emails = {item["email"] for item in result["items"]}
    assert emails == {"carol@beta.io"}


@pytest.mark.asyncio
async def test_list_users_unknown_role_filter_returns_empty(session_factory) -> None:
    """An unknown role string short-circuits to empty rather than 500."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(
            db, role_filter="not-a-role"
        )
    assert result["items"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_list_users_status_filter_inactive(session_factory) -> None:
    """``status='inactive'`` returns the disabled user."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(
            db, status_filter="inactive"
        )
    emails = {item["email"] for item in result["items"]}
    assert emails == {"carol@beta.io"}


@pytest.mark.asyncio
async def test_list_users_status_filter_unverified(session_factory) -> None:
    """``status='unverified'`` returns users without email_verified."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(
            db, status_filter="unverified"
        )
    emails = {item["email"] for item in result["items"]}
    assert emails == {"bob@acme.io"}


@pytest.mark.asyncio
async def test_list_users_status_filter_superadmin(session_factory) -> None:
    """``status='superadmin'`` returns the platform superadmin."""
    await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(
            db, status_filter="superadmin"
        )
    emails = {item["email"] for item in result["items"]}
    assert emails == {"alice@acme.io"}


@pytest.mark.asyncio
async def test_list_users_pagination(session_factory) -> None:
    """``limit`` + ``offset`` slice the result. Total is full count."""
    await _seed(session_factory)
    async with session_factory() as db:
        page1 = await admin_users_search_service.list_users(db, limit=2, offset=0)
        page2 = await admin_users_search_service.list_users(db, limit=2, offset=2)
    assert page1["total"] == 4
    assert page2["total"] == 4
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    # No overlap.
    page1_ids = {item["id"] for item in page1["items"]}
    page2_ids = {item["id"] for item in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
async def test_list_users_row_shape(session_factory) -> None:
    """List row carries the contracted fields including ``orgs`` array."""
    ids = await _seed(session_factory)
    async with session_factory() as db:
        result = await admin_users_search_service.list_users(db, q="alice")
    row = result["items"][0]
    assert row["id"] == ids["alice_id"]
    assert row["email"] == "alice@acme.io"
    assert row["username"] == "alice"
    assert row["display_name"] == "Alice Smith"
    assert row["is_superadmin"] is True
    assert row["is_active"] is True
    assert row["email_verified"] is True
    assert row["mfa_enabled"] is True
    assert isinstance(row["orgs"], list) and len(row["orgs"]) == 1
    assert row["orgs"][0]["org_id"] == ids["org_a_id"]
    assert row["orgs"][0]["name"] == "Acme"
    assert row["orgs"][0]["role"] == "owner"


# ── get_user_detail ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_detail_returns_user_org_and_audit(session_factory) -> None:
    """Detail carries org membership and recent audit events."""
    ids = await _seed(session_factory)
    async with session_factory() as db:
        detail = await admin_users_search_service.get_user_detail(
            db, user_id=ids["alice_id"]
        )
    assert detail["id"] == ids["alice_id"]
    assert detail["email"] == "alice@acme.io"
    assert detail["orgs"][0]["org_id"] == ids["org_a_id"]
    # Detail-only fields present.
    assert "password_set" in detail
    assert "sessions_invalidated_at" in detail
    # Alice authored two audit events; recent_audit_events is non-empty.
    assert len(detail["recent_audit_events"]) == 2
    types = {e["event_type"] for e in detail["recent_audit_events"]}
    assert types == {"admin.org.delete", "admin.org.subscription.override"}


@pytest.mark.asyncio
async def test_get_user_detail_missing_raises_not_found(session_factory) -> None:
    """Nonexistent user id raises NotFoundError."""
    await _seed(session_factory)
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await admin_users_search_service.get_user_detail(db, user_id=99999)


@pytest.mark.asyncio
async def test_get_user_detail_user_without_audit_events(session_factory) -> None:
    """A user with no authored events has an empty recent_audit_events list."""
    ids = await _seed(session_factory)
    async with session_factory() as db:
        detail = await admin_users_search_service.get_user_detail(
            db, user_id=ids["bob_id"]
        )
    assert detail["recent_audit_events"] == []
