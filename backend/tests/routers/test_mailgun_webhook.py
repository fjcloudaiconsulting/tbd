"""Router tests for the public Mailgun delivery-webhook sink.

POST /api/v1/webhooks/mailgun — covers the required matrix (spec
2026-07-20, rulings W2/W3/W5/W6/W7/W10/W11):

- valid signature ``delivered`` → 200 and the row's delivery_status set;
- invalid signature → 401 (no write); missing signature fields → 400;
- unset signing key → 404 (fail closed);
- oversized content-length → dropped (413);
- out-of-order: ``complained`` then late ``delivered`` stays ``complained``;
- ``bounced_temporary`` then ``delivered`` → ``delivered``;
- duplicate identical event → no-op (idempotent);
- unknown ``broadcast_id`` → 200, no write; unmatched email → 200, no write;
- ``failed`` + missing severity → ``bounced_permanent``;
- ignored event (``opened``) → 200, no write;
- case-insensitive email match;
- ``v:broadcast_id`` arrives as a STRING and is parsed.

Signatures are computed with the same HMAC formula the verifier uses. No
real HTTP to Mailgun; Redis is unconfigured in tests so the replay-token
helper fails open (every token is first-sight) and precedence is what makes
duplicates idempotent.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import app.routers.webhooks as webhooks_module
from app.config import settings as app_settings
from app.deps import get_session_factory
from app.models import Base
from app.models.email_broadcast import (
    BroadcastStatus,
    EmailBroadcast,
    EmailBroadcastRecipient,
    RecipientStatus,
)
from app.rate_limit import limiter
from app.routers.webhooks import router as webhooks_router


SIGNING_KEY = "test-webhook-signing-key-abc123"


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


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _set_signing_key(monkeypatch):
    """Default every test to a configured signing key. The unset-key test
    overrides this back to ``""``."""
    monkeypatch.setattr(app_settings, "mailgun_webhook_signing_key", SIGNING_KEY)


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch) -> AsyncIterator[AsyncClient]:
    # The router calls get_session_factory() directly (not via Depends), so
    # patch the symbol the router imported.
    monkeypatch.setattr(
        webhooks_module, "get_session_factory", lambda: session_factory
    )

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.include_router(webhooks_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── body builders ───────────────────────────────────────────────────────


def _sign(timestamp: str, token: str, key: str = SIGNING_KEY) -> str:
    return hmac.new(
        key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()


def _event_data(
    *,
    event: str = "delivered",
    severity: object = None,
    recipient: str = "foo@x.io",
    broadcast_id: object = "1",
    timestamp: float | None = None,
) -> dict:
    ed: dict = {
        "event": event,
        "recipient": recipient,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "user-variables": {"broadcast_id": broadcast_id},
    }
    if severity is not None:
        ed["severity"] = severity
    return ed


def _body(
    event_data: dict,
    *,
    token: str | None = None,
    timestamp: str | None = None,
    key: str = SIGNING_KEY,
    signature: str | None = None,
) -> dict:
    ts = timestamp if timestamp is not None else str(int(time.time()))
    tok = token if token is not None else f"tok-{time.time_ns()}"
    sig = signature if signature is not None else _sign(ts, tok, key)
    return {
        "signature": {"timestamp": ts, "token": tok, "signature": sig},
        "event-data": event_data,
    }


# ── seeding ──────────────────────────────────────────────────────────────


async def _seed(
    factory,
    *,
    email: str = "foo@x.io",
    delivery_status: str | None = None,
) -> tuple[int, int]:
    async with factory() as db:
        bc = EmailBroadcast(
            subject="Subject",
            body_template="Hello {first_name}",
            segment="active_verified",
            status=BroadcastStatus.SENDING,
        )
        db.add(bc)
        await db.commit()
        rec = EmailBroadcastRecipient(
            broadcast_id=bc.id,
            email=email,
            first_name="Foo",
            status=RecipientStatus.SENT,
            delivery_status=delivery_status,
        )
        db.add(rec)
        await db.commit()
        return bc.id, rec.id


async def _status(factory, rec_id: int) -> str | None:
    async with factory() as db:
        rec = await db.get(EmailBroadcastRecipient, rec_id)
        return rec.delivery_status


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_delivered_sets_status(client, session_factory):
    bid, rid = await _seed(session_factory)
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(event="delivered", broadcast_id=str(bid))),
    )
    assert resp.status_code == 200
    assert await _status(session_factory, rid) == "delivered"


@pytest.mark.asyncio
async def test_invalid_signature_401_no_write(client, session_factory):
    bid, rid = await _seed(session_factory)
    body = _body(
        _event_data(broadcast_id=str(bid)),
        signature="deadbeef" * 8,  # wrong signature
    )
    resp = await client.post("/api/v1/webhooks/mailgun", json=body)
    assert resp.status_code == 401
    assert resp.content == b""
    assert await _status(session_factory, rid) is None


@pytest.mark.asyncio
async def test_missing_signature_fields_400(client, session_factory):
    await _seed(session_factory)
    # signature object present but missing 'token'
    ts = str(int(time.time()))
    body = {
        "signature": {"timestamp": ts, "signature": "x"},
        "event-data": _event_data(),
    }
    resp = await client.post("/api/v1/webhooks/mailgun", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_signature_object_400(client, session_factory):
    await _seed(session_factory)
    resp = await client.post(
        "/api/v1/webhooks/mailgun", json={"event-data": _event_data()}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_malformed_json_400(client):
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_unset_key_404(client, session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "mailgun_webhook_signing_key", "")
    bid, rid = await _seed(session_factory)
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(broadcast_id=str(bid))),
    )
    assert resp.status_code == 404
    assert await _status(session_factory, rid) is None


@pytest.mark.asyncio
async def test_oversized_content_length_dropped(client):
    # Advertise an oversized body — dropped on the content-length precheck.
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(256 * 1024 + 1),
        },
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_out_of_order_complained_then_delivered_stays(
    client, session_factory
):
    bid, rid = await _seed(session_factory)
    # complained first (rank 4)
    r1 = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(event="complained", broadcast_id=str(bid))),
    )
    assert r1.status_code == 200
    assert await _status(session_factory, rid) == "complained"
    # late delivered (rank 2) must NOT override
    r2 = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(event="delivered", broadcast_id=str(bid))),
    )
    assert r2.status_code == 200
    assert await _status(session_factory, rid) == "complained"


@pytest.mark.asyncio
async def test_temporary_then_delivered_upgrades(client, session_factory):
    bid, rid = await _seed(session_factory)
    r1 = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(
            _event_data(
                event="failed", severity="temporary", broadcast_id=str(bid)
            )
        ),
    )
    assert r1.status_code == 200
    assert await _status(session_factory, rid) == "bounced_temporary"
    r2 = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(event="delivered", broadcast_id=str(bid))),
    )
    assert r2.status_code == 200
    assert await _status(session_factory, rid) == "delivered"


@pytest.mark.asyncio
async def test_duplicate_event_is_noop(client, session_factory):
    bid, rid = await _seed(session_factory)
    ed = _event_data(event="delivered", broadcast_id=str(bid))
    r1 = await client.post("/api/v1/webhooks/mailgun", json=_body(ed))
    assert r1.status_code == 200
    first_updated = None
    async with session_factory() as db:
        rec = await db.get(EmailBroadcastRecipient, rid)
        first_updated = rec.delivery_updated_at
    # Same delivered event again → equal rank → no-op (no status change).
    r2 = await client.post("/api/v1/webhooks/mailgun", json=_body(ed))
    assert r2.status_code == 200
    async with session_factory() as db:
        rec = await db.get(EmailBroadcastRecipient, rid)
        assert rec.delivery_status == "delivered"
        # updated_at untouched by the no-op second apply
        assert rec.delivery_updated_at == first_updated


@pytest.mark.asyncio
async def test_unknown_broadcast_id_200_no_write(client, session_factory):
    bid, rid = await _seed(session_factory)
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(event="delivered", broadcast_id="999999")),
    )
    assert resp.status_code == 200
    assert await _status(session_factory, rid) is None


@pytest.mark.asyncio
async def test_unmatched_email_200_no_write(client, session_factory):
    bid, rid = await _seed(session_factory, email="foo@x.io")
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(
            _event_data(
                event="delivered",
                recipient="someone-else@x.io",
                broadcast_id=str(bid),
            )
        ),
    )
    assert resp.status_code == 200
    assert await _status(session_factory, rid) is None


@pytest.mark.asyncio
async def test_failed_missing_severity_is_permanent(client, session_factory):
    bid, rid = await _seed(session_factory)
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(
            _event_data(event="failed", severity=None, broadcast_id=str(bid))
        ),
    )
    assert resp.status_code == 200
    assert await _status(session_factory, rid) == "bounced_permanent"


@pytest.mark.asyncio
async def test_ignored_event_opened_200_no_write(client, session_factory):
    bid, rid = await _seed(session_factory)
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(_event_data(event="opened", broadcast_id=str(bid))),
    )
    assert resp.status_code == 200
    assert await _status(session_factory, rid) is None


@pytest.mark.asyncio
async def test_case_insensitive_email_match(client, session_factory):
    # Stored lowercase; event recipient upper/mixed case must still match.
    bid, rid = await _seed(session_factory, email="foo@x.io")
    resp = await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(
            _event_data(
                event="delivered",
                recipient="Foo@X.io",
                broadcast_id=str(bid),
            )
        ),
    )
    assert resp.status_code == 200
    assert await _status(session_factory, rid) == "delivered"


@pytest.mark.asyncio
async def test_broadcast_id_string_is_parsed(client, session_factory):
    bid, rid = await _seed(session_factory)
    # broadcast_id explicitly a string (as Mailgun echoes v: vars)
    ed = _event_data(event="delivered", broadcast_id=str(bid))
    assert isinstance(ed["user-variables"]["broadcast_id"], str)
    resp = await client.post("/api/v1/webhooks/mailgun", json=_body(ed))
    assert resp.status_code == 200
    assert await _status(session_factory, rid) == "delivered"


@pytest.mark.asyncio
async def test_handler_never_logs_raw_email(client, session_factory, monkeypatch):
    """Spot-check W10: the handler must not bind the raw recipient email into
    any structlog call. Capture every event-dict passed to the logger and
    assert the address never appears as a value."""
    captured: list[dict] = []

    real_info = webhooks_module.logger.ainfo
    real_warn = webhooks_module.logger.awarning

    async def _capture_info(event, **kw):
        captured.append({"event": event, **kw})
        return await real_info(event, **kw)

    async def _capture_warn(event, **kw):
        captured.append({"event": event, **kw})
        return await real_warn(event, **kw)

    monkeypatch.setattr(webhooks_module.logger, "ainfo", _capture_info)
    monkeypatch.setattr(webhooks_module.logger, "awarning", _capture_warn)

    bid, rid = await _seed(session_factory, email="secret@person.io")
    # Exercise the happy path + a no-match path (both breadcrumb).
    await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(
            _event_data(
                event="delivered",
                recipient="secret@person.io",
                broadcast_id=str(bid),
            )
        ),
    )
    await client.post(
        "/api/v1/webhooks/mailgun",
        json=_body(
            _event_data(
                event="delivered",
                recipient="secret@person.io",
                broadcast_id="999999",
            )
        ),
    )
    assert captured, "expected at least one breadcrumb"
    for rec in captured:
        for value in rec.values():
            assert "secret@person.io" not in str(value)
