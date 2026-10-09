import asyncio

import pytest

import app.services.admin_dashboard_service as admin_dashboard_service
from app.services.admin_dashboard_service import (
    _probe_db,
    build_dashboard_payload,
)


class FakeAsyncSession:
    def __init__(self, scalar_results: list[int | None] | None = None):
        self.scalar_results = list(scalar_results or [])
        self.scalar_calls = 0
        self.active_scalars = 0
        self.max_active_scalars = 0

    async def scalar(self, _statement):
        self.scalar_calls += 1
        self.active_scalars += 1
        self.max_active_scalars = max(
            self.max_active_scalars, self.active_scalars
        )
        await asyncio.sleep(0)
        self.active_scalars -= 1
        return self.scalar_results.pop(0)


class ExecuteRaisesSession:
    async def execute(self, _statement):
        raise RuntimeError("db unavailable")


class SlowExecuteSession:
    async def execute(self, _statement):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_probe_db_returns_error_name_when_query_raises() -> None:
    result = await _probe_db(ExecuteRaisesSession())

    assert result == {"ok": False, "error": "RuntimeError"}


@pytest.mark.asyncio
async def test_probe_db_reports_timeout(monkeypatch) -> None:
    monkeypatch.setattr(admin_dashboard_service, "PROBE_TIMEOUT_SECONDS", 0.001)

    result = await _probe_db(SlowExecuteSession())

    assert result == {"ok": False, "error": "timeout"}


@pytest.mark.asyncio
async def test_build_dashboard_payload_collects_kpis_and_health(monkeypatch) -> None:
    db = FakeAsyncSession([17, 42, 12, 3])

    async def fake_probe_db(_db):
        return {"ok": True, "latency_ms": 4.2}

    monkeypatch.setattr(admin_dashboard_service, "_probe_db", fake_probe_db)

    payload = await build_dashboard_payload(db)

    assert payload == {
        "kpis": {
            "total_orgs": 17,
            "total_users": 42,
            "active_subscriptions": 12,
            "signups_last_7d": 3,
        },
        "health": {
            "db": {"ok": True, "latency_ms": 4.2},
        },
    }
    assert db.scalar_calls == 4
    assert db.max_active_scalars == 1


@pytest.mark.asyncio
async def test_build_dashboard_payload_coerces_missing_scalar_results_to_zero(
    monkeypatch,
) -> None:
    db = FakeAsyncSession([None, None, None, None])

    async def fake_probe_db(_db):
        return {"ok": True, "latency_ms": 1.1}

    monkeypatch.setattr(admin_dashboard_service, "_probe_db", fake_probe_db)

    payload = await build_dashboard_payload(db)

    assert payload["kpis"] == {
        "total_orgs": 0,
        "total_users": 0,
        "active_subscriptions": 0,
        "signups_last_7d": 0,
    }
