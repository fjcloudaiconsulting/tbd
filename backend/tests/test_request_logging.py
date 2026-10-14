"""INFRA-105 step 3: request context and the access line, through the production app objects.

F9 context on every line, F10 no leak between requests, F11 the access line, F21 asyncio handler.
The API app is the real ``app.main.app`` (DB swapped for sqlite, no lifespan); two requests on one
``httpx.AsyncClient`` run in one task, which is where a context that leaks shows (TestClient runs each
call in a fresh task and cannot see it).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import logs
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token
from app.services import tag_service

CLIENT_IP = "203.0.113.9"
TAGS = "/api/v1/tags"


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def real_app(session_factory):
    from app.main import app

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    limiter.reset()
    yield app
    app.dependency_overrides.clear()
    limiter.reset()


@pytest_asyncio.fixture
async def bearer(session_factory) -> dict[str, str]:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="u", email="u@acme.io", password_hash="x", role=Role.OWNER,
            is_active=True, email_verified=True,
        )
        db.add(user)
        await db.commit()
        token = create_access_token(user.id, org.id, user.role.value)
        return {"Authorization": f"Bearer {token}", "user_id": str(user.id)}


def client_for(app, **kwargs) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(CLIENT_IP, 1234), **kwargs)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def access(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line["logger"] == "app.access"]


@pytest.fixture
def inside(monkeypatch):
    """A line logged from inside the request, between the middleware and the handler."""

    async def listing(db, org_id, q=None):
        logging.getLogger("app.tests.inside").info("inside")
        return []

    monkeypatch.setattr(tag_service, "list_org_tags", listing)


# F9: request_id on every line of the request; user_id bound in deps lands on the access line.
async def test_f9_every_line_has_the_request_id_and_the_access_line_the_user(
    real_app, bearer, log_lines, inside
):
    async with client_for(real_app) as client:
        response = await client.get(
            TAGS, headers={"Authorization": bearer["Authorization"], "X-Request-Id": "rid-abc"}
        )

    lines = log_lines()
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "rid-abc"
    assert [line["msg"] for line in lines] == ["inside", "request"]
    assert {line["request_id"] for line in lines} == {"rid-abc"}
    assert access(lines)[0]["user_id"] == int(bearer["user_id"])


# F10: the context of one request never reaches the next one on the same task.
async def test_f10_a_second_request_does_not_inherit_the_first_users_context(
    real_app, bearer, log_lines
):
    async with client_for(real_app) as client:
        first = await client.get(TAGS, headers={"Authorization": bearer["Authorization"]})
        second = await client.get(TAGS)  # no credential: nothing binds a user

    one, two = access(log_lines())
    assert (first.status_code, second.status_code) == (200, 401)
    assert "user_id" in one and "user_id" not in two and "org_id" not in two
    assert one["request_id"] != two["request_id"]


# F11: one access line per request, with ids, and never the path value, the query or the client IP.
async def test_f11_one_access_line_with_the_template_and_the_span_ids(
    real_app, bearer, log_lines, spans
):
    async with client_for(real_app) as client:
        response = await client.patch(
            "/api/v1/tags/987654?code=SECRET",
            json={"name": "x"},
            headers={"Authorization": bearer["Authorization"]},
        )

    lines = log_lines()
    (line,) = access(lines)
    server = next(s for s in spans() if s.kind == SpanKind.SERVER)
    assert response.status_code == line["status"] == 404
    assert (line["msg"], line["level"], line["method"]) == ("request", "INFO", "PATCH")
    assert line["route"] == "/api/v1/tags/{tag_id}"
    assert isinstance(line["duration_ms"], (int, float))
    assert line["trace_id"] == format(server.context.trace_id, "032x")
    assert line["span_id"] == format(server.context.span_id, "016x")
    text = "\n".join(str(line) for line in lines)
    assert "987654" not in text and "SECRET" not in text and CLIENT_IP not in text


async def test_f11_an_unmatched_request_has_no_route(real_app, log_lines):
    async with client_for(real_app) as client:
        await client.get("/no/such/thing/1234")

    (line,) = access(log_lines())
    assert line["status"] == 404 and "route" not in line
    assert "1234" not in str(line)


async def test_f11_the_health_probe_is_debug_only(real_app, log_lines):
    async with client_for(real_app) as client:
        await client.get("/health")
        await client.get("/ready")

    lines = access(log_lines())
    assert [line["level"] for line in lines] == ["DEBUG", "DEBUG"]


async def test_f11_an_unhandled_error_still_yields_one_access_line_with_status_500(
    real_app, bearer, log_lines, monkeypatch
):
    async def boom(db, org_id, q=None):
        raise RuntimeError("cannot list")

    monkeypatch.setattr(tag_service, "list_org_tags", boom)
    async with client_for(real_app, raise_app_exceptions=False) as client:
        response = await client.get(
            TAGS, headers={"Authorization": bearer["Authorization"], "X-Request-Id": "rid-500"}
        )

    (line,) = access(log_lines())
    assert response.status_code == 500
    assert line["status"] == 500 and line["request_id"] == "rid-500"
    assert line["route"] == TAGS and line["trace_id"]


async def test_f11_a_post_to_the_mcp_app_yields_one_access_line_with_a_request_id(log_lines):
    from app import mcp_main

    async with client_for(mcp_main.app) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

    lines = log_lines()
    (line,) = access(lines)
    assert response.status_code == 401
    assert line["status"] == 401 and line["method"] == "POST"
    assert line["request_id"] == response.headers["x-request-id"]
    assert line["route"] == "/mcp"  # Starlette 1.7 sets scope["route"] on a plain Route
    assert {entry["request_id"] for entry in lines} == {line["request_id"]}


async def test_a_query_secret_reaches_no_line(real_app, log_lines):
    async with client_for(real_app) as client:
        await client.get("/api/v1/orgs/invitations/preview?token=SECRET")

    lines = log_lines()
    assert access(lines)  # the positive control: the request was logged at all
    assert "SECRET" not in str(lines)


# F21: the loop's exception handler logs the class and frames, never the message or the future.
async def test_f21_the_asyncio_handler_logs_the_class_not_the_message(log_lines):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(logs.asyncio_exception_handler)
    try:
        error = RuntimeError("SEC" + "RET-leak")
        loop.call_exception_handler(
            {"message": "Task exception was never retrieved SEC" + "RET-ctx",
             "exception": error, "future": "SEC" + "RET-future"}
        )
    finally:
        loop.set_exception_handler(previous)

    (line,) = [line for line in log_lines() if line["msg"] == "asyncio.unhandled"]
    assert line["exc"][0]["type"] == "builtins.RuntimeError"
    assert "SECRET" not in str(line)
