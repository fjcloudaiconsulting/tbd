"""TBD-443: recover and detect a missing platform superadmin.

* ``promote_superadmin`` grants the flag only to an active, email-verified,
  MFA-enrolled target; with an active superadmin present it also needs
  ``additional=True``. Every refusal leaves the row untouched and writes a
  ``failure`` audit row; a grant commits with its ``success`` row and
  invalidates the target's existing sessions.
* The CLI maps outcomes to exit codes, asks for a typed confirmation, and
  never prints the email it was given.
* The scheduler probe and the dashboard count only ACTIVE superadmins.
"""
from __future__ import annotations

import argparse
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models.audit_event as audit_event_module
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.services import superadmin_recovery_service as svc
from app.services.admin_dashboard_service import build_dashboard_payload
from app.services.scheduler.jobs.superadmin_floor import run_superadmin_floor_probe
from scripts import promote_superadmin as cli

EMAIL = "target@example.test"


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _user(factory, *, email=EMAIL, superadmin=False, active=True, verified=True, mfa=True) -> int:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username=email.split("@")[0], email=email,
            password_hash="x", role=Role.OWNER, is_superadmin=superadmin,
            is_active=active, email_verified=verified, mfa_enabled=mfa,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _get(factory, user_id: int) -> User:
    async with factory() as db:
        return await db.get(User, user_id)


async def _events(factory) -> list[AuditEvent]:
    async with factory() as db:
        return list((await db.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars())


async def _promote(factory, user_id: int, *, additional=False) -> dict:
    return await svc.promote_superadmin(
        factory, user_id=user_id, operator="ops-oncall", reason="INC-1", additional=additional
    )


# ══ Service ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_promotes_on_a_zero_superadmin_install_with_one_success_row(factory):
    uid = await _user(factory)

    result = await _promote(factory, uid)

    user = await _get(factory, uid)
    assert result["status"] == "promoted"
    assert user.is_superadmin is True
    assert user.sessions_invalidated_at is not None, "existing sessions must not inherit the flag"
    (event,) = await _events(factory)
    assert event.event_type == "admin.superadmin.promoted"
    assert event.outcome.value == "success"
    assert event.actor_user_id is None and event.actor_email == "ops-oncall"
    assert event.target_org_id == user.org_id
    # Ids and counts only: never the email.
    assert event.detail == {
        "target_user_id": uid,
        "via": "operator_cli",
        "additional": False,
        "prior_active_superadmin_count": 0,
        "reason": "INC-1",
    }
    assert EMAIL not in str(event.detail)


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"active": False}, svc.CODE_INACTIVE),
        ({"verified": False}, svc.CODE_EMAIL_UNVERIFIED),
        ({"mfa": False}, svc.CODE_MFA_NOT_ENABLED),
        ({"active": False, "superadmin": True}, svc.CODE_INACTIVE),
    ],
)
@pytest.mark.asyncio
async def test_refusals_leave_the_row_alone_and_write_a_failure_row(factory, kwargs, code):
    uid = await _user(factory, **kwargs)
    before = await _get(factory, uid)

    with pytest.raises(svc.PromotionRefused) as exc:
        await _promote(factory, uid)

    after = await _get(factory, uid)
    assert exc.value.code == code
    assert after.is_superadmin == before.is_superadmin
    assert after.sessions_invalidated_at is None
    (event,) = await _events(factory)
    assert event.outcome.value == "failure"
    assert event.detail["code"] == code
    assert event.detail["target_user_id"] == uid


@pytest.mark.asyncio
async def test_an_existing_active_superadmin_needs_additional(factory):
    await _user(factory, email="root@example.test", superadmin=True)
    uid = await _user(factory)

    with pytest.raises(svc.PromotionRefused) as exc:
        await _promote(factory, uid)
    assert exc.value.code == svc.CODE_SUPERADMIN_EXISTS
    assert (await _get(factory, uid)).is_superadmin is False

    result = await _promote(factory, uid, additional=True)
    assert result["status"] == "promoted"
    assert result["prior_active_superadmin_count"] == 1
    assert (await _get(factory, uid)).is_superadmin is True


@pytest.mark.asyncio
async def test_an_inactive_superadmin_does_not_count_as_existing(factory):
    """The one that matters: zero ACTIVE superadmins is the outage."""
    await _user(factory, email="gone@example.test", superadmin=True, active=False)
    uid = await _user(factory)

    assert (await _promote(factory, uid))["status"] == "promoted"


@pytest.mark.asyncio
async def test_already_superadmin_is_a_no_op_without_a_row(factory):
    uid = await _user(factory, superadmin=True)

    assert (await _promote(factory, uid))["status"] == "already_superadmin"
    assert (await _get(factory, uid)).sessions_invalidated_at is None
    assert await _events(factory) == []


@pytest.mark.asyncio
async def test_unknown_user_raises_lookup_error(factory):
    with pytest.raises(LookupError):
        await _promote(factory, 999)
    assert await _events(factory) == []


def test_event_type_is_documented_in_the_taxonomy():
    assert svc.PROMOTED_EVENT_TYPE in (audit_event_module.__doc__ or "")


# ══ CLI ════════════════════════════════════════════════════════════════════


def _args(**kwargs) -> argparse.Namespace:
    base = dict(
        email=EMAIL, user_id=None, operator="ops-oncall", reason="INC-1",
        additional=False, yes=True,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.fixture
def run(monkeypatch, factory):
    monkeypatch.setattr(cli, "async_session", factory)

    async def _run(args, confirm=None):
        return await cli._run(args, confirm=confirm or (lambda _p: pytest.fail("prompted")))

    return _run


@pytest.mark.asyncio
async def test_cli_promotes_by_email_and_never_prints_it(run, factory, capsys):
    uid = await _user(factory)

    assert await run(_args(email="  Target@Example.TEST ")) == cli.EXIT_OK

    assert (await _get(factory, uid)).is_superadmin is True
    out = capsys.readouterr()
    assert f"user_id={uid}" in out.err
    assert "example.test" not in (out.out + out.err).lower()


@pytest.mark.asyncio
async def test_cli_exit_codes(run, factory, capsys):
    assert await run(_args()) == cli.EXIT_NOT_FOUND
    uid = await _user(factory, mfa=False)
    assert await run(_args(user_id=uid, email=None)) == cli.EXIT_REFUSED
    assert svc.CODE_MFA_NOT_ENABLED in capsys.readouterr().err
    assert (await _get(factory, uid)).is_superadmin is False


@pytest.mark.asyncio
async def test_cli_typed_confirmation(run, factory, monkeypatch):
    uid = await _user(factory)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert await run(_args(yes=False), confirm=lambda _p: "nope") == cli.EXIT_NOT_CONFIRMED
    assert (await _get(factory, uid)).is_superadmin is False
    assert await _events(factory) == []

    assert await run(_args(yes=False), confirm=lambda _p: f" {uid}\n") == cli.EXIT_OK
    assert (await _get(factory, uid)).is_superadmin is True


@pytest.mark.asyncio
async def test_cli_without_a_terminal_requires_yes(run, factory, monkeypatch):
    uid = await _user(factory)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert await run(_args(yes=False)) == cli.EXIT_NOT_CONFIRMED
    assert (await _get(factory, uid)).is_superadmin is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--email", EMAIL, "--reason", "x"],  # no operator
        ["--email", EMAIL, "--operator", "o"],  # no reason
        ["--email", EMAIL, "--operator", "o", "--reason", "x" * 201],
        ["--email", EMAIL, "--user-id", "1", "--operator", "o", "--reason", "x"],
    ],
)
def test_cli_argument_validation(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == cli.EXIT_USAGE


# ══ Detection ══════════════════════════════════════════════════════════════


def _points(reader, name):
    data = reader.get_metrics_data()
    return [p.value for rm in (data.resource_metrics if data else []) for sm in rm.scope_metrics
            for m in sm.metrics if m.name == name for p in m.data.data_points]


def _events_logged(log_lines, prefix="platform.superadmin."):
    return [line for line in log_lines() if str(line.get("msg", "")).startswith(prefix)]


@pytest.mark.asyncio
async def test_probe_alarms_at_zero_on_a_populated_install(factory, log_lines, metric_reader):
    await _user(factory, superadmin=True, active=False)

    assert await run_superadmin_floor_probe(factory) == 0

    (line,) = _events_logged(log_lines)
    assert line["msg"] == "platform.superadmin.none"
    assert line["level"] == "ERROR"
    assert _points(metric_reader, "tbd.platform.active_superadmins") == [0]


@pytest.mark.asyncio
async def test_probe_warns_at_one_and_is_quiet_at_two(factory, log_lines, metric_reader):
    await _user(factory, superadmin=True)
    assert await run_superadmin_floor_probe(factory) == 1
    assert [e["msg"] for e in _events_logged(log_lines)] == ["platform.superadmin.single"]

    await _user(factory, email="second@example.test", superadmin=True)
    assert await run_superadmin_floor_probe(factory) == 2
    assert len(_events_logged(log_lines)) == 1
    assert _points(metric_reader, "tbd.platform.active_superadmins") == [2]


@pytest.mark.asyncio
async def test_probe_is_quiet_on_an_empty_install(factory, log_lines):
    assert await run_superadmin_floor_probe(factory) == 0
    assert _events_logged(log_lines) == []


@pytest.mark.asyncio
async def test_probe_swallows_a_database_error(log_lines):
    def broken():
        raise RuntimeError("db down")

    assert await run_superadmin_floor_probe(broken) is None
    assert [e["msg"] for e in _events_logged(log_lines, "scheduler.superadmin_floor")] == [
        "scheduler.superadmin_floor.failed"
    ]


@pytest.mark.asyncio
async def test_dashboard_counts_only_active_superadmins(factory, monkeypatch):
    import app.services.admin_dashboard_service as dashboard

    async def ok(_db):
        return {"ok": True}

    monkeypatch.setattr(dashboard, "_probe_db", ok)
    await _user(factory, email="a@example.test", superadmin=True)
    await _user(factory, email="b@example.test", superadmin=True, active=False)
    await _user(factory, email="c@example.test")

    async with factory() as db:
        payload = await build_dashboard_payload(db)
    assert payload["kpis"]["active_superadmins"] == 1
