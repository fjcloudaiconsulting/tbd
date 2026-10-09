"""INFRA-105: own OpenTelemetry spans and FastAPI's native HTTP metrics (aws-infra docs/architecture.md,
Telemetry). The privacy fences prove that query-string secrets (the invite ``token``, the Google OAuth
``code`` and ``state``), bound SQL values and exception messages reach no exported span."""

from __future__ import annotations

import asyncio
import datetime
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterable

import pymysql
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import tracing
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.middleware.tracing import TracingMiddleware
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_invitation_token
from app.services import invitation_service
from app.services.scheduler import loop as scheduler_loop_module
from app.services.scheduler import runner as R
from app.services.scheduler.base import JobResult

SERVER_KEYS = {"http.request.method", "http.route", "http.response.status_code"}
SECRET = "SECRET"
STREAMED: list[int] = []


# ── helpers ────────────────────────────────────────────────────────────────


def _server(spans: Iterable[ReadableSpan]) -> ReadableSpan:
    found = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(found) == 1, f"expected exactly one SERVER span, got {[s.name for s in found]}"
    return found[0]


def _haystack(span: ReadableSpan) -> str:
    """Everything a span exports, as text."""
    parts = [span.name, str(span.status.description), str(span.context.trace_state)]
    parts += [f"{k}={v}" for k, v in (span.attributes or {}).items()]
    for event in span.events:
        parts.append(event.name)
        parts += [f"{k}={v}" for k, v in (event.attributes or {}).items()]
    for link in span.links:
        parts += [f"{k}={v}" for k, v in (link.attributes or {}).items()]
    parts += [f"{k}={v}" for k, v in span.resource.attributes.items()]
    return "\n".join(parts)


def _assert_absent(spans: list[ReadableSpan], *needles: str) -> None:
    assert spans
    for span in spans:
        text_ = _haystack(span)
        for needle in needles:
            assert needle not in text_, f"{needle!r} exported by span {span.name!r}"


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
    """The production app object, its DB swapped for the test's sqlite. No lifespan (no `with`)."""
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


async def _seed_invitation(factory) -> tuple[str, str]:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id, username="owner", email="owner@acme.io", password_hash="x",
            role=Role.OWNER, is_active=True, email_verified=True,
        )
        db.add(owner)
        await db.commit()
        email = "invitee-secret@acme.io"
        inv = await invitation_service.create_invitation(
            db, org_id=org.id, created_by=owner.id, email=email, role=Role.MEMBER,
        )
        await db.commit()
        return create_invitation_token(inv.id, inv.email), email


# ── privacy fences on the real app ─────────────────────────────────────────


async def test_invitation_preview_with_literal_secret_exports_one_clean_server_span(real_app, spans):
    response = TestClient(real_app).get(f"/api/v1/orgs/invitations/preview?token={SECRET}")
    assert response.status_code == 410

    finished = spans()
    server = _server(finished)
    assert set(server.attributes) == SERVER_KEYS
    assert server.attributes["http.route"] == "/api/v1/orgs/invitations/preview"
    assert server.name == "GET /api/v1/orgs/invitations/preview"
    _assert_absent(finished, SECRET)


async def test_invitation_preview_with_a_real_token_keeps_token_and_email_out_of_every_span(
    real_app, session_factory, spans
):
    token, email = await _seed_invitation(session_factory)
    response = TestClient(real_app).get(f"/api/v1/orgs/invitations/preview?token={token}")
    assert response.status_code == 200
    assert response.json()["email"] == email

    finished = spans()
    server = _server(finished)
    assert set(server.attributes) == SERVER_KEYS
    sql = [s for s in finished if s.kind == SpanKind.CLIENT]
    # The lookup that binds the invitee's email ran inside the request, as a child of it.
    assert any("users" in s.attributes["db.query.text"] for s in sql)
    assert all(s.parent is not None and s.parent.span_id == server.context.span_id for s in sql)
    _assert_absent(finished, token, email)


async def test_google_callback_keeps_code_and_state_out_of_every_span(
    real_app, spans, monkeypatch
):
    from tests.routers.test_auth_google_callback_errors import _patch_httpx

    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    # The token exchange fails, so the handler records a failure audit row (SQL in the span).
    _patch_httpx(monkeypatch, token_status=400)
    state = "STATE-SECRET-0123456789"
    client = TestClient(real_app, follow_redirects=False)
    client.cookies.set("oauth_state", state)

    response = client.get(f"/api/v1/auth/google/callback?code={SECRET}&state={state}")
    assert response.status_code in (302, 307)

    finished = spans()
    server = _server(finished)
    assert set(server.attributes) == SERVER_KEYS
    assert server.attributes["http.route"] == "/api/v1/auth/google/callback"
    assert any(s.kind == SpanKind.CLIENT for s in finished), "the audit row's SQL should be traced"
    _assert_absent(finished, SECRET, state)


async def test_an_unhandled_error_quoting_the_secret_exports_only_its_class(
    real_app, spans, monkeypatch
):
    async def boom(db, *, token):
        raise ValueError(f"cannot preview {token}")

    monkeypatch.setattr(invitation_service, "preview_invitation", boom)
    response = TestClient(real_app, raise_server_exceptions=False).get(
        f"/api/v1/orgs/invitations/preview?token={SECRET}"
    )
    assert response.status_code == 500

    finished = spans()
    server = _server(finished)
    assert set(server.attributes) == SERVER_KEYS | {"error.type"}
    assert server.attributes["error.type"] == "ValueError"
    assert server.attributes["http.response.status_code"] == 500
    assert server.status.status_code.name == "ERROR"
    assert server.events == ()
    _assert_absent(finished, SECRET)


def test_health_paths_get_no_span_and_no_metric(real_app, spans, metric_reader):
    client = TestClient(real_app)
    for path in sorted(tracing.HEALTH_PATHS):
        client.get(path)
    assert [s for s in spans() if s.kind == SpanKind.SERVER] == []
    assert _duration_points(metric_reader) == []


# ── native HTTP metrics ────────────────────────────────────────────────────


def _duration_points(reader) -> list:
    data = reader.get_metrics_data()
    points = []
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == "http.server.request.duration":
                    points += list(metric.data.data_points)
    return points


def test_http_metrics_carry_only_the_allowlisted_attributes(real_app, metric_reader):
    TestClient(real_app).get(f"/api/v1/orgs/invitations/preview?token={SECRET}")
    points = _duration_points(metric_reader)
    assert points, "FastAPI's native duration histogram should record on our MeterProvider"
    for point in points:
        assert set(point.attributes) <= tracing.HTTP_METRIC_ATTRIBUTES
        assert SECRET not in str(point.attributes)
    assert {p.attributes.get("http.route") for p in points} == {"/api/v1/orgs/invitations/preview"}


# ── SERVER span middleware on a minimal app ────────────────────────────────


def _mini_app() -> FastAPI:
    # Native tracing off, as in app.main: it would add its own SERVER span.
    app = FastAPI(telemetry={"tracing": False})
    app.add_middleware(TracingMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.get("/stream")
    async def stream():
        async def body():
            for chunk in (b"a", b"b"):
                await asyncio.sleep(0.01)
                STREAMED.append(time.time_ns())
                yield chunk

        return StreamingResponse(body())

    @app.get("/down")
    async def down():
        return JSONResponse({"detail": "x"}, status_code=503)

    return app


def test_the_span_ends_after_a_streamed_body_not_at_its_headers(spans):
    STREAMED.clear()
    assert TestClient(_mini_app()).get("/stream").content == b"ab"
    server = _server(spans())
    assert server.end_time >= STREAMED[-1]


def test_importing_the_app_registers_sdk_providers():
    """conftest calls configure() itself, so only a fresh interpreter shows app.main does."""
    code = (
        "import app.main\n"
        "from opentelemetry import metrics, trace\n"
        "from opentelemetry.sdk.metrics import MeterProvider\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "print('PROVIDERS', isinstance(trace.get_tracer_provider(), TracerProvider),"
        " isinstance(metrics.get_meter_provider(), MeterProvider))"
    )
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("OTEL_")}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    ).stdout.splitlines()
    providers = [line.split()[1:] for line in out if line.startswith("PROVIDERS ")]
    assert providers == [["True", "True"]]


def test_a_handled_5xx_is_an_error_named_by_its_status(spans):
    TestClient(_mini_app()).get("/down")
    server = _server(spans())
    assert server.attributes["error.type"] == "503"
    assert server.status.status_code.name == "ERROR"


def test_unmatched_path_is_unmatched_and_a_4xx_is_not_an_error(spans):
    TestClient(_mini_app()).get(f"/nope/{SECRET}")
    server = _server(spans())
    assert server.name == "GET unmatched"
    assert server.attributes["http.route"] == "unmatched"
    assert server.attributes["http.response.status_code"] == 404
    assert "error.type" not in server.attributes
    assert server.status.status_code.name == "UNSET"
    _assert_absent([server], SECRET)


def test_route_template_not_path_and_unknown_methods_are_other(spans):
    client = TestClient(_mini_app())
    client.get("/items/42")
    client.request("PROPFIND", "/items/42")
    first, second = [s for s in spans() if s.kind == SpanKind.SERVER]
    assert first.name == "GET /items/{item_id}"
    assert second.attributes["http.request.method"] == "_OTHER"


def test_only_traceparent_is_read_from_the_request(spans):
    trace_id, parent_id = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"
    TestClient(_mini_app()).get(
        "/items/1",
        headers={
            "traceparent": f"00-{trace_id}-{parent_id}-01",
            "tracestate": f"vendor={SECRET}",
            "baggage": f"user={SECRET}",
        },
    )
    server = _server(spans())
    assert format(server.context.trace_id, "032x") == trace_id
    assert format(server.parent.span_id, "016x") == parent_id
    assert len(server.context.trace_state) == 0
    _assert_absent([server], SECRET)


def test_a_not_sampled_remote_parent_cannot_hide_the_request(spans):
    """flags=00 from a client must not drop the SERVER span and its SQL: the root sampler decides."""
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    TestClient(_mini_app()).get(
        "/items/1", headers={"traceparent": f"00-{trace_id}-b7ad6b7169203331-00"}
    )
    server = _server(spans())
    assert format(server.context.trace_id, "032x") == trace_id  # still stitched (residual)
    assert server.context.trace_flags.sampled


# ── SQL spans ──────────────────────────────────────────────────────────────


async def test_sql_span_carries_the_statement_never_its_bound_values(session_factory, spans):
    needle = "secret-value@example.com"
    with tracing.span("outer", SpanKind.INTERNAL, {}) as outer:
        async with session_factory() as db:
            await db.execute(select(User).where(User.email == needle))
    sql = [s for s in spans() if s.kind == SpanKind.CLIENT]
    assert len(sql) == 1
    assert sql[0].name == "SELECT"
    assert set(sql[0].attributes) == {"db.system.name", "db.query.text"}
    assert sql[0].attributes["db.system.name"] == "sqlite"
    assert sql[0].parent.span_id == outer.get_span_context().span_id
    _assert_absent(sql, needle)


async def test_sql_outside_any_span_is_not_traced(session_factory, spans):
    async with session_factory() as db:
        await db.execute(text("SELECT 1"))
    assert spans() == []


async def test_a_failing_statement_ends_its_span_as_an_error(session_factory, spans):
    async with session_factory() as db:
        db.add(Organization(name="Acme", billing_cycle_day=1))
        await db.commit()
    needle = "dup-secret@example.com"
    with pytest.raises(IntegrityError):
        with tracing.span("outer", SpanKind.INTERNAL, {}):
            async with session_factory() as db:
                await db.execute(
                    text("INSERT INTO organizations (id, name) VALUES (1, :name)"), {"name": needle}
                )
    finished = spans()
    sql = [s for s in finished if s.kind == SpanKind.CLIENT]
    assert len(sql) == 1
    assert sql[0].status.status_code.name == "ERROR"
    assert sql[0].status.description == "IntegrityError"
    assert sql[0].attributes["error.type"] == "IntegrityError"
    assert sql[0].events == ()
    # str(ctx.sqlalchemy_exception) would carry "[parameters: ('dup-secret@...',)]".
    _assert_absent(finished, needle)


def test_a_mysql_error_quoting_a_row_value_reaches_the_span_as_class_and_errno(spans):
    """sqlite's messages never quote values; MySQL's do ("Duplicate entry '<value>' ...")."""
    needle = "secret@example.com"
    driver = pymysql.err.IntegrityError(1062, f"Duplicate entry '{needle}' for key 'users.email'")

    class _Ctx:
        original_exception = driver
        sqlalchemy_exception = IntegrityError("INSERT ...", {"email": needle}, driver)

    with tracing.span("outer", SpanKind.INTERNAL, {}):
        _Ctx.execution_context = type("_Exec", (), {})()
        _Ctx.execution_context._tbd_span = tracing.trace.get_tracer(__name__).start_span(
            "INSERT", kind=SpanKind.CLIENT
        )
        tracing._handle_error(_Ctx())
    (sql,) = [s for s in spans() if s.kind == SpanKind.CLIENT]
    assert sql.status.description == "IntegrityError errno=1062"
    _assert_absent([sql], needle)


def test_error_summary_is_the_class_and_mysql_errno_never_the_message():
    driver = pymysql.err.IntegrityError(1062, "Duplicate entry 'a@b.c' for key 'users.email'")
    wrapped = IntegrityError("INSERT ...", {"email": "a@b.c"}, driver)
    assert tracing.error_summary(wrapped) == "IntegrityError errno=1062"
    assert tracing.error_summary(driver) == "IntegrityError errno=1062"
    assert tracing.error_summary(KeyError(42)) == "KeyError"
    assert tracing.error_summary(ValueError("a@b.c")) == "ValueError"


# ── exporter gate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env, exported",
    [
        ({}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}, False),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9/v1/traces",
          "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:9/v1/metrics"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9", "OTEL_TRACES_EXPORTER": "none",
          "OTEL_METRICS_EXPORTER": "None "}, False),
    ],
)
def test_nothing_is_exported_without_an_endpoint(monkeypatch, env, exported):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    provider = tracing._provider("tbd-api")
    meter_provider = tracing._meter_provider("tbd-api")
    try:
        # Private reads: neither SDK provider has a public API that lists its processors or readers.
        assert len(provider._active_span_processor._span_processors) == int(exported)
        assert len(meter_provider._metric_readers) == int(exported)
    finally:
        provider.shutdown()
        meter_provider.shutdown()


# ── scheduler ──────────────────────────────────────────────────────────────


class _Job:
    def __init__(self, job_type: str, *, boom: bool = False):
        self.job_type = job_type
        self.setting_key = f"{job_type}_key"
        self._boom = boom

    async def is_due(self, db, org, today):
        return True

    async def run(self, db, org, today):
        if self._boom:
            raise RuntimeError(f"kaboom {SECRET}")
        return JobResult.ok({})


async def _one_org(factory) -> None:
    async with factory() as db:
        db.add(Organization(name="A", billing_cycle_day=1))
        await db.commit()


async def test_scheduler_tick_and_jobs_are_spans(session_factory, spans, monkeypatch):
    await _one_org(session_factory)

    async def enabled(db, org_id, key):
        return True

    async def no_audit(**kwargs):
        return 1

    monkeypatch.setattr(R.org_settings, "get_bool", enabled)
    monkeypatch.setattr(R, "record_run", no_audit)
    stop = asyncio.Event()

    async def one_tick(today, *, lock_ttl, max_orgs=None):
        stop.set()
        await R.run_all_due(
            today, session_factory=session_factory,
            registry=[_Job("good"), _Job("bad", boom=True)],
        )

    monkeypatch.setattr(scheduler_loop_module, "run_one_tick", one_tick)
    await scheduler_loop_module.scheduler_loop(stop, tick_seconds=0, lock_ttl=1)

    finished = spans()
    tick = [s for s in finished if s.name == "scheduler.tick"]
    assert len(tick) == 1 and tick[0].parent is None
    good = [s for s in finished if s.name == "job good"]
    bad = [s for s in finished if s.name == "job bad"]
    assert len(good) == 1 and len(bad) == 1
    for job_span in good + bad:
        assert job_span.parent.span_id == tick[0].context.span_id
    assert dict(good[0].attributes) == {"job.kind": "good"}
    assert dict(bad[0].attributes) == {"job.kind": "bad", "error.type": "RuntimeError"}
    assert bad[0].status.status_code.name == "ERROR"
    assert bad[0].events == ()
    # The org lookup ran inside the tick.
    assert any(
        s.kind == SpanKind.CLIENT and s.parent.span_id == tick[0].context.span_id for s in finished
    )
    _assert_absent(finished, "kaboom", SECRET)


async def test_each_tick_is_its_own_trace_and_a_failed_one_is_an_error(spans, monkeypatch):
    stop = asyncio.Event()
    calls = []

    async def tick(today, *, lock_ttl, max_orgs=None):
        calls.append(today)
        if len(calls) == 1:
            raise ConnectionError(f"redis {SECRET}")
        stop.set()

    monkeypatch.setattr(scheduler_loop_module, "run_one_tick", tick)
    await scheduler_loop_module.scheduler_loop(stop, tick_seconds=0, lock_ttl=1)
    # A span around the whole loop would never end in production (the loop runs forever).
    first, second = [s for s in spans() if s.name == "scheduler.tick"]
    assert first.context.trace_id != second.context.trace_id
    assert first.attributes["error.type"] == "ConnectionError"
    assert first.status.status_code.name == "ERROR"
    assert "error.type" not in second.attributes
    _assert_absent([first, second], SECRET)


async def test_the_api_token_expiry_reminder_is_a_job_span(spans, monkeypatch):
    calls = []

    async def acquire(ttl):
        return True

    async def run_all_due(today, *, max_orgs=None):
        return None

    async def reminders(*, now):
        calls.append(now)

    monkeypatch.setattr(scheduler_loop_module, "acquire_tick_lock", acquire)
    monkeypatch.setattr(scheduler_loop_module, "run_all_due", run_all_due)
    monkeypatch.setattr(scheduler_loop_module, "run_api_token_expiry_reminders", reminders)
    await scheduler_loop_module.run_one_tick(datetime.date(2026, 10, 8), lock_ttl=1)
    assert len(calls) == 1
    (job,) = [s for s in spans() if s.name == "job api_token_expiry"]
    assert dict(job.attributes) == {"job.kind": "api_token_expiry"}


async def test_the_oauth_client_purge_is_a_job_span(spans, monkeypatch):
    inside = []

    async def acquire(ttl):
        return True

    async def run_all_due(today, *, max_orgs=None):
        return None

    async def reminders(*, now):
        return None

    async def purge():
        inside.append(tracing.trace.get_current_span().get_span_context().span_id)

    monkeypatch.setattr(scheduler_loop_module, "acquire_tick_lock", acquire)
    monkeypatch.setattr(scheduler_loop_module, "run_all_due", run_all_due)
    monkeypatch.setattr(scheduler_loop_module, "run_api_token_expiry_reminders", reminders)
    monkeypatch.setattr(scheduler_loop_module, "run_oauth_client_purge", purge)
    await scheduler_loop_module.run_one_tick(datetime.date(2026, 10, 8), lock_ttl=1)
    (job,) = [s for s in spans() if s.name == "job oauth_client_purge"]
    assert dict(job.attributes) == {"job.kind": "oauth_client_purge"}
    # The purge runs inside its own span, not beside it.
    assert inside == [job.context.span_id]


@pytest.mark.parametrize(
    "env, root",
    [
        ({}, "AlwaysOnSampler"),
        ({"OTEL_TRACES_SAMPLER": "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG": "0.25"},
         "TraceIdRatioBased{0.25}"),
        ({"OTEL_TRACES_SAMPLER": "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG": "x"},
         "AlwaysOnSampler"),
        ({"OTEL_TRACES_SAMPLER": "always_off"}, "AlwaysOffSampler"),
    ],
)
def test_the_sampler_follows_the_env_and_its_root_decides_for_a_not_sampled_remote(
    monkeypatch, env, root
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    description = tracing._sampler().get_description()
    assert f"root:{root}" in description
    assert f"remoteParentNotSampled:{root}" in description
