"""Router tests for the superadmin email-broadcast API (Task 5, spec
``2026-07-18-admin-email-broadcast-design.md``).

Pins the architect-locked invariants for this layer:

- Every endpoint requires ``is_superadmin`` (403 otherwise).
- ``POST /`` returns a live ``recipient_count`` for the segment.
- ``POST /{id}/send`` runs the 5-check gate IN ORDER: 409 not-draft, then
  422 dry_run_required / confirm_subject_mismatch / confirm_count_mismatch /
  recipient_cap_exceeded.
- The happy path (dry-run then send) drains every recipient and a second
  send on the same broadcast is refused with 409.
- Audit ``detail`` never carries a recipient email address (Ruling 13).

The two send paths are mocked so no real Mailgun call happens anywhere in this
file: the router's own dry-run path (``app.routers.admin_broadcasts.send_email``,
single recipient) and the drain engine's Mailgun batch call
(``app.services.broadcast_service.send_batch``, one call per batch). The drain launched by ``POST /{id}/send``
runs on a bare ``asyncio.create_task`` (Ruling 1) rather than FastAPI's
``BackgroundTasks``, so ``TestClient`` does not block for it to finish —
tests poll ``GET /{id}`` until the broadcast reaches a terminal status.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings as app_settings
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.email_broadcast import (
    SEGMENT_ACTIVE_VERIFIED,
    EmailBroadcast,
    EmailBroadcastRecipient,
    RecipientStatus,
)
from app.models.user import Organization, Role, User
from app.routers import admin_broadcasts as admin_broadcasts_module
from app.routers.admin_broadcasts import router as admin_broadcasts_router
from app.security import hash_password
from app.services import broadcast_service
from tests.factories import make_test_app


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A FILE-backed (not ``:memory:``) sqlite engine, deliberately.

    This test suite has a genuinely concurrent background asyncio task (the
    drain launched by ``POST /send``) whose own session runs at the same
    wall-clock time as the test's polling ``GET`` requests. An in-memory
    ``StaticPool`` engine hands out the exact same single physical
    connection to every session, so two concurrently-open sessions share one
    SQLite transactional slot and can wedge each other (observed empirically
    as the drain permanently stalling at N-1 of N recipients). A temp-file
    database with the default pool gives each ``session_factory()`` call its
    own real connection, which is what actual concurrent use looks like.
    """
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
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
        os.unlink(path)


@pytest.fixture(autouse=True)
def _fast_pacing(monkeypatch):
    """No real sleeping between drain sends."""
    monkeypatch.setattr(broadcast_service.settings, "broadcast_pacing_seconds", 0)


@pytest.fixture(autouse=True)
def _clean_registries():
    """Isolate the module-level drain registries between tests."""
    broadcast_service._ACTIVE_DRAINS.clear()
    broadcast_service._DRAIN_TASKS.clear()
    yield
    broadcast_service._ACTIVE_DRAINS.clear()
    broadcast_service._DRAIN_TASKS.clear()


@pytest.fixture(autouse=True)
def _mock_send_email(monkeypatch):
    """Mock the two send paths: the router's dry-run ``send_email`` (single
    recipient = the calling superadmin) and the drain engine's ``send_batch``
    (Mailgun batch sending, 2026-07-19 revision). Returns both mocks with
    ``return_value=True``; ``drain`` is the ``send_batch`` mock (one call per
    batch, NOT per recipient)."""
    dry_run_mock = AsyncMock(return_value=True)
    drain_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_broadcasts_module, "send_email", dry_run_mock)
    monkeypatch.setattr(broadcast_service, "send_batch", drain_mock)
    return {"dry_run": dry_run_mock, "drain": drain_mock}


def _make_app(session_factory, current_user_resolver):
    return make_test_app(
        session_factory,
        routers=admin_broadcasts_router,
        current_user=current_user_resolver,
        override_session_factory=True,
    )


async def _seed(factory) -> dict:
    """Seed a superadmin, a plain (non-superadmin) user, 3 active+verified
    "customer" users (the segment), 1 inactive, and 1 unverified. The actor
    users themselves are seeded ``email_verified=False`` so they never
    pollute the ``active_verified`` segment count."""
    async with factory() as db:
        org = Organization(name="Platform", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        superadmin = User(
            org_id=org.id,
            username="root",
            email="root@platform.io",
            first_name="Root",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=False,
        )
        plain = User(
            org_id=org.id,
            username="user",
            email="u@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=False,
        )
        db.add_all([superadmin, plain])

        recipients = []
        for i in range(3):
            recipients.append(
                User(
                    org_id=org.id,
                    username=f"cust{i}",
                    email=f"cust{i}@customer.io",
                    first_name=f"Cust{i}",
                    password_hash=hash_password("pw-1234567"),
                    role=Role.MEMBER,
                    is_superadmin=False,
                    is_active=True,
                    email_verified=True,
                )
            )
        inactive_user = User(
            org_id=org.id,
            username="inactive",
            email="inactive@customer.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=False,
            email_verified=True,
        )
        unverified_user = User(
            org_id=org.id,
            username="unverified",
            email="unverified@customer.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=False,
        )
        db.add_all(recipients + [inactive_user, unverified_user])
        await db.commit()
        return {"org_id": org.id, "sa_id": superadmin.id, "plain_id": plain.id}


def _superadmin_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()

    return resolve


def _plain_user_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.username == "user"))
            ).scalar_one()

    return resolve


def _create_draft(client, subject="Hi there", body="Hi {first_name}, welcome back."):
    res = client.post(
        "/api/v1/admin/broadcasts",
        json={
            "subject": subject,
            "body_template": body,
            "segment": SEGMENT_ACTIVE_VERIFIED,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


async def _wait_for_terminal_status(client, broadcast_id, timeout=5.0):
    """Poll GET /{id} until the broadcast reaches a terminal status.

    The drain launched by POST /send runs as a bare tracked asyncio task
    (Ruling 1), not FastAPI's BackgroundTasks, so TestClient's synchronous
    call for /send does not block until the drain finishes. TestClient
    shares the pytest-asyncio test's event loop, so the polling wait MUST be
    an ``await asyncio.sleep`` (not a blocking ``time.sleep``) — a blocking
    sleep would freeze the single shared loop and starve the drain task of
    any chance to run its own awaits.
    """
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        res = client.get(f"/api/v1/admin/broadcasts/{broadcast_id}")
        assert res.status_code == 200
        body = res.json()
        if body["status"] in ("completed", "failed"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"broadcast never reached a terminal status: {body}")


# ── superadmin gate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_endpoint_requires_superadmin(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        draft = client.post(
            "/api/v1/admin/broadcasts",
            json={
                "subject": "Hi",
                "body_template": "Hi {first_name},",
                "segment": SEGMENT_ACTIVE_VERIFIED,
            },
        )
        assert draft.status_code == 403

        assert client.get("/api/v1/admin/broadcasts").status_code == 403
        assert client.get("/api/v1/admin/broadcasts/1").status_code == 403
        assert client.get("/api/v1/admin/broadcasts/1/preview").status_code == 403
        assert client.post("/api/v1/admin/broadcasts/1/dry-run").status_code == 403
        assert (
            client.post(
                "/api/v1/admin/broadcasts/1/send",
                json={"confirm_subject": "x", "confirm_recipient_count": 0},
            ).status_code
            == 403
        )
        assert client.post("/api/v1/admin/broadcasts/1/resume").status_code == 403


# ── create ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_live_recipient_count(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)
    assert draft["status"] == "draft"
    assert draft["recipient_count"] == 3
    assert draft["total_recipients"] is None


# ── send gate, in order ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_without_dry_run_returns_422(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)
        res = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "dry_run_required"


@pytest.mark.asyncio
async def test_send_wrong_subject_returns_422(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)
        assert client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/dry-run"
        ).status_code == 200
        res = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": "not the subject",
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "confirm_subject_mismatch"


@pytest.mark.asyncio
async def test_send_wrong_count_returns_422(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)
        assert client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/dry-run"
        ).status_code == 200
        res = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"] + 41,
            },
        )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "confirm_count_mismatch"


@pytest.mark.asyncio
async def test_send_over_cap_returns_422(session_factory, monkeypatch):
    await _seed(session_factory)
    monkeypatch.setattr(app_settings, "broadcast_max_recipients", 0)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)
        assert client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/dry-run"
        ).status_code == 200
        res = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "recipient_cap_exceeded"


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        pytest.param(
            "Hi there",
            "Hi {first_name}, enjoy 50% off this week.",
            id="stray_percent_in_body",
        ),
        pytest.param(
            "Enjoy 50% off",
            "Hi {first_name}, welcome back.",
            id="stray_percent_in_subject",
        ),
        pytest.param(
            "Hi there",
            "Hi {first_name}, %recipient.bogus%",
            id="unknown_recipient_token",
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_with_hazardous_template_returns_422(
    session_factory, subject, body
):
    """The MA1 token guard runs at the SEND GATE, synchronously.

    Before this, a stray ``%`` sailed through create/preview/dry-run, ``POST
    /send`` returned 200 with status ``sending``, and only then did the
    background drain raise and flip the broadcast to ``failed`` — the
    operator saw a success followed by an unexplained failure. Now the
    request itself fails and the broadcast stays ``draft``, so the copy can
    be fixed and re-sent.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client, subject=subject, body=body)
        # Create + dry-run both still succeed; the gate is the send call.
        assert client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/dry-run"
        ).status_code == 200

        res = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
        assert res.status_code == 422, res.text
        assert res.json()["detail"]["code"] == "invalid_template_token"

        # Nothing was materialized or claimed: still a draft.
        after = client.get(f"/api/v1/admin/broadcasts/{draft['id']}").json()
        assert after["status"] == "draft"


# ── happy path + idempotency + audit ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_then_send_drains_all_recipients(session_factory, _mock_send_email):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)

        dry_run_res = client.post(f"/api/v1/admin/broadcasts/{draft['id']}/dry-run")
        assert dry_run_res.status_code == 200
        assert dry_run_res.json()["dry_run_sent_at"] is not None
        # Dry-run sends exactly once, to the caller's own address.
        assert _mock_send_email["dry_run"].await_count == 1
        dry_run_call = _mock_send_email["dry_run"].await_args_list[0]
        assert dry_run_call.args[0] == "root@platform.io"

        send_res = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
        assert send_res.status_code == 200
        assert send_res.json()["status"] in ("sending", "completed")

        final = await _wait_for_terminal_status(client, draft["id"])
        assert final["status"] == "completed"
        assert final["sent_count"] == 3
        assert final["failed_count"] == 0
        assert final["total_recipients"] == 3

        # Second send on the same (now non-draft) broadcast is refused.
        second = client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "broadcast_not_draft"

    # Every recipient row is SENT.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(EmailBroadcastRecipient.status).where(
                    EmailBroadcastRecipient.broadcast_id == draft["id"]
                )
            )
        ).scalars().all()
    assert rows and all(s == RecipientStatus.SENT for s in rows)
    # Batch sending: the 3 recipients go out in ONE Mailgun batch call, not one
    # call per recipient. That single call's ``to_list`` covers all three.
    assert _mock_send_email["drain"].await_count == 1
    batch_to_list = _mock_send_email["drain"].await_args_list[0].args[0]
    assert len(batch_to_list) == 3


@pytest.mark.asyncio
async def test_audit_events_carry_no_recipient_email(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        draft = _create_draft(client)
        client.post(f"/api/v1/admin/broadcasts/{draft['id']}/dry-run")
        client.post(
            f"/api/v1/admin/broadcasts/{draft['id']}/send",
            json={
                "confirm_subject": draft["subject"],
                "confirm_recipient_count": draft["recipient_count"],
            },
        )
        await _wait_for_terminal_status(client, draft["id"])
        client.post(f"/api/v1/admin/broadcasts/{draft['id']}/resume")
        # Let the (no-op, nothing pending) resume drain settle before the
        # TestClient context tears down its portal — an in-flight task
        # abruptly cancelled by portal shutdown can take the shared
        # in-memory sqlite connection down with it (StaticPool has exactly
        # one physical connection), which would otherwise flake later
        # queries in THIS test with "no such table".
        await _wait_for_terminal_status(client, draft["id"])

    recipient_emails = [
        "cust0@customer.io",
        "cust1@customer.io",
        "cust2@customer.io",
        "inactive@customer.io",
        "unverified@customer.io",
    ]
    async with session_factory() as db:
        events = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type.in_(
                        [
                            "broadcast.create",
                            "broadcast.dry_run",
                            "broadcast.send",
                            "broadcast.resume",
                        ]
                    )
                )
            )
        ).scalars().all()
    assert len(events) == 4
    for ev in events:
        assert "email" not in (ev.detail or {})
        blob = json.dumps(ev.detail)
        for addr in recipient_emails:
            assert addr not in blob
        assert ev.target_org_id is None


# ── delivery counts + recipients endpoint (Task 5, W9) ───────────────────


async def _seed_broadcast_with_mixed_delivery_status(factory) -> int:
    """Seed one broadcast + 6 recipient rows directly (no real send), with
    mixed ``delivery_status``: two delivered, one bounced_permanent, one
    bounced_temporary, one complained, one NULL (no webhook event yet)."""
    async with factory() as db:
        broadcast = EmailBroadcast(
            subject="Delivery status test",
            body_template="Hi {first_name},",
            segment=SEGMENT_ACTIVE_VERIFIED,
            total_recipients=6,
        )
        db.add(broadcast)
        await db.commit()
        await db.refresh(broadcast)

        statuses = [
            "delivered",
            "delivered",
            "bounced_permanent",
            "bounced_temporary",
            "complained",
            None,
        ]
        for i, delivery_status in enumerate(statuses):
            db.add(
                EmailBroadcastRecipient(
                    broadcast_id=broadcast.id,
                    email=f"recip{i}@customer.io",
                    first_name=f"Recip{i}",
                    status=RecipientStatus.SENT,
                    delivery_status=delivery_status,
                    delivery_updated_at=(
                        None if delivery_status is None else datetime(2026, 7, 20)
                    ),
                )
            )
        await db.commit()
        return broadcast.id


@pytest.mark.asyncio
async def test_get_broadcast_returns_delivery_counts(session_factory):
    await _seed(session_factory)
    broadcast_id = await _seed_broadcast_with_mixed_delivery_status(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/broadcasts/{broadcast_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["delivered_count"] == 2
    assert body["bounced_count"] == 1
    assert body["soft_bounced_count"] == 1
    assert body["complained_count"] == 1


@pytest.mark.asyncio
async def test_list_broadcasts_returns_delivery_counts(session_factory):
    """The LIST endpoint must also carry the derived counts, via the single
    grouped query (no N+1)."""
    await _seed(session_factory)
    broadcast_id = await _seed_broadcast_with_mixed_delivery_status(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/broadcasts")
    assert res.status_code == 200
    items = {item["id"]: item for item in res.json()["items"]}
    assert items[broadcast_id]["delivered_count"] == 2
    assert items[broadcast_id]["bounced_count"] == 1
    assert items[broadcast_id]["soft_bounced_count"] == 1
    assert items[broadcast_id]["complained_count"] == 1


@pytest.mark.asyncio
async def test_recipients_endpoint_requires_superadmin(session_factory):
    await _seed(session_factory)
    broadcast_id = await _seed_broadcast_with_mixed_delivery_status(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/broadcasts/{broadcast_id}/recipients")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_recipients_endpoint_returns_rows_with_delivery_status(session_factory):
    await _seed(session_factory)
    broadcast_id = await _seed_broadcast_with_mixed_delivery_status(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/broadcasts/{broadcast_id}/recipients")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 6
    by_email = {item["email"]: item for item in body["items"]}
    assert by_email["recip0@customer.io"]["delivery_status"] == "delivered"
    assert by_email["recip2@customer.io"]["delivery_status"] == "bounced_permanent"
    assert by_email["recip3@customer.io"]["delivery_status"] == "bounced_temporary"
    assert by_email["recip4@customer.io"]["delivery_status"] == "complained"
    assert by_email["recip5@customer.io"]["delivery_status"] is None
