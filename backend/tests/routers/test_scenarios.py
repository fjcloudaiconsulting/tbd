"""Router tests for the Plans simulation sandbox (spec 2026-05-22).

Architect-locked invariants pinned here:

- CRUD round-trip with Pydantic discriminator validating params per
  scenario_type (trip + purchase fully wired; retirement + custom
  ship minimal stubs).
- Horizon-cap validator:
    * trip / purchase / custom with horizon_months > 120 → 422
    * retirement with horizon_months > 480 → 422
    * retirement with horizon_months = 480 → 201 (allowed)
    * trip with horizon_months = 121 → 422 (one over the cap)
- ``POST /simulate`` writes ``projection_json`` and
  ``projection_computed_at`` and returns a populated projection.
- Cross-user isolation: user B cannot read / patch / delete /
  simulate user A's scenarios (404 on every path).
- Empty body to ``POST /simulate`` defaults to engine=analytic.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.scenario import Scenario, ScenarioType
from app.models.user import Organization, Role, User
from app.routers.scenarios import router as scenarios_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


def _make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(scenarios_router)
    return app


async def _seed_users_and_account(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        u_a = User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        u_b = User(
            org_id=org.id,
            username="bob",
            email="bob@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add_all([u_a, u_b])
        await db.commit()
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add(at)
        await db.commit()
        acc = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Main checking",
            balance=Decimal("5000.00"),
            currency="EUR",
            is_active=True,
            is_default=True,
            opening_balance=Decimal("5000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        db.add(acc)
        await db.commit()
        return {
            "org_id": org.id,
            "alice_id": u_a.id,
            "bob_id": u_b.id,
            "account_id": acc.id,
        }


def _resolver_for(email: str):
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one()
    return resolve


def _trip_payload(account_id: int, *, name: str = "Lisbon trip", horizon: int = 24) -> dict:
    return {
        "name": name,
        "scenario_type": "trip",
        "horizon_months": horizon,
        "params": {
            "scenario_type": "trip",
            "destination": "Lisbon, Portugal",
            "start_date": "2026-09-15",
            "duration_days": 10,
            "currency": "EUR",
            "transport_cost": "450.00",
            "accommodation_per_night": "85.00",
            "daily_budget": "70.00",
            "one_off_extras": [
                {
                    "label": "Castelo tickets",
                    "amount": "15.00",
                    "on_date": "2026-09-17",
                },
            ],
            "source_account_id": account_id,
        },
    }


def _purchase_payload(account_id: int, *, name: str = "Used car", horizon: int = 36) -> dict:
    return {
        "name": name,
        "scenario_type": "purchase",
        "horizon_months": horizon,
        "params": {
            "scenario_type": "purchase",
            "subtype": "car",
            "label": "Replacement car",
            "target_date": "2027-03-01",
            "currency": "EUR",
            "total_price": "22000.00",
            "down_payment": "8000.00",
            "down_payment_account_id": account_id,
            "financing": {
                "principal": "14000.00",
                "annual_rate_pct": "6.5",
                "term_months": 60,
                "first_payment_date": "2027-04-01",
                "payment_account_id": account_id,
            },
        },
    }


def _retirement_payload(account_id: int, *, name: str = "At 62", horizon: int = 240) -> dict:
    return {
        "name": name,
        "scenario_type": "retirement",
        "horizon_months": horizon,
        "params": {
            "scenario_type": "retirement",
            "target_retirement_date": "2048-06-01",
            "currency": "EUR",
            "monthly_contribution": "600.00",
            "contribution_account_id": account_id,
            "target_balance": "750000.00",
            "annual_return_pct": "5.0",
        },
    }


def _custom_payload(*, name: str = "Sabbatical 2028", horizon: int = 36) -> dict:
    return {
        "name": name,
        "scenario_type": "custom",
        "horizon_months": horizon,
        "params": {
            "scenario_type": "custom",
            "label": "Sabbatical year, no salary 2028",
            "events": [],
        },
    }


# ── CRUD happy path per scenario_type ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_trip_round_trip(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/scenarios",
            json=_trip_payload(seeds["account_id"]),
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "Lisbon trip"
    assert body["scenario_type"] == "trip"
    assert body["horizon_months"] == 24
    assert body["is_active"] is True
    assert body["user_id"] == seeds["alice_id"]
    assert body["org_id"] == seeds["org_id"]
    assert body["params_json"]["destination"] == "Lisbon, Portugal"


@pytest.mark.asyncio
async def test_create_purchase_round_trip(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/scenarios",
            json=_purchase_payload(seeds["account_id"]),
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["scenario_type"] == "purchase"
    assert body["params_json"]["financing"]["term_months"] == 60


@pytest.mark.asyncio
async def test_create_retirement_minimal(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/scenarios",
            json=_retirement_payload(seeds["account_id"]),
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["scenario_type"] == "retirement"


@pytest.mark.asyncio
async def test_create_custom_minimal(session_factory):
    await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/scenarios",
            json=_custom_payload(),
        )
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_create_rejects_type_params_mismatch(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    payload = _trip_payload(seeds["account_id"])
    payload["scenario_type"] = "purchase"
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        res = client.post("/api/v1/scenarios", json=payload)
    # Pydantic v2 surfaces this as a 422 either via discriminator or
    # via the outer model_validator; both are acceptable signals.
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_list_default_hides_archived(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        client.post("/api/v1/scenarios", json=_trip_payload(seeds["account_id"], name="Visible"))
        created = client.post(
            "/api/v1/scenarios",
            json=_trip_payload(seeds["account_id"], name="Archived"),
        ).json()
        client.patch(
            f"/api/v1/scenarios/{created['id']}",
            json={"is_active": False},
        )
        default_list = client.get("/api/v1/scenarios").json()
        archived_list = client.get(
            "/api/v1/scenarios", params={"include_archived": "true"}
        ).json()
    default_names = {r["name"] for r in default_list}
    archived_names = {r["name"] for r in archived_list}
    assert "Visible" in default_names
    assert "Archived" not in default_names
    assert "Archived" in archived_names


@pytest.mark.asyncio
async def test_patch_updates_name_and_horizon(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        res = client.patch(
            f"/api/v1/scenarios/{created['id']}",
            json={"name": "Lisbon ALT", "horizon_months": 36},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "Lisbon ALT"
    assert body["horizon_months"] == 36


@pytest.mark.asyncio
async def test_patch_rejects_cross_type_params(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        # Trip row receiving a purchase params blob: must be rejected.
        res = client.patch(
            f"/api/v1/scenarios/{created['id']}",
            json={"params": _purchase_payload(seeds["account_id"])["params"]},
        )
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_delete_removes_row(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        res = client.delete(f"/api/v1/scenarios/{created['id']}")
        assert res.status_code == 204
        fetch = client.get(f"/api/v1/scenarios/{created['id']}")
    assert fetch.status_code == 404


# ── Horizon-cap validator ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trip_horizon_121_rejected(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    payload = _trip_payload(seeds["account_id"], horizon=121)
    with TestClient(app) as client:
        res = client.post("/api/v1/scenarios", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_purchase_horizon_121_rejected(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    payload = _purchase_payload(seeds["account_id"], horizon=121)
    with TestClient(app) as client:
        res = client.post("/api/v1/scenarios", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_retirement_horizon_480_allowed(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    payload = _retirement_payload(seeds["account_id"], horizon=480)
    with TestClient(app) as client:
        res = client.post("/api/v1/scenarios", json=payload)
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_retirement_horizon_481_rejected(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    payload = _retirement_payload(seeds["account_id"], horizon=481)
    with TestClient(app) as client:
        res = client.post("/api/v1/scenarios", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_custom_horizon_121_rejected(session_factory):
    await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    payload = _custom_payload(horizon=121)
    with TestClient(app) as client:
        res = client.post("/api/v1/scenarios", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_patch_horizon_over_cap_rejected(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        res = client.patch(
            f"/api/v1/scenarios/{created['id']}",
            json={"horizon_months": 121},
        )
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_simulate_horizon_over_cap_rejected(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        res = client.post(
            f"/api/v1/scenarios/{created['id']}/simulate",
            json={"engine": "analytic", "horizon_months": 121, "options": {}},
        )
    assert res.status_code == 422, res.text


# ── Simulate happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_simulate_writes_projection(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        res = client.post(
            f"/api/v1/scenarios/{created['id']}/simulate",
            json={"engine": "analytic", "options": {}},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["projection_json"] is not None
    assert body["projection_engine"] == "analytic_v1"
    assert body["projection_computed_at"] is not None
    projection = body["projection_json"]
    assert projection["engine_name"] == "analytic_v1"
    assert projection["horizon_months"] == 24
    assert len(projection["per_account_series"]) == 1
    series = projection["per_account_series"][0]
    assert series["account_id"] == seeds["account_id"]
    assert len(series["points"]) == 24
    assert projection["verdict"]["color"] in {"green", "yellow", "red"}


@pytest.mark.asyncio
async def test_simulate_empty_body_defaults_to_analytic(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app = _make_app(session_factory, _resolver_for("alice@acme.io"))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
        # No body at all — should still default to analytic.
        res = client.post(f"/api/v1/scenarios/{created['id']}/simulate")
    assert res.status_code == 200, res.text
    assert res.json()["projection_engine"] == "analytic_v1"


# ── Cross-user isolation (per-user scoping) ─────────────────────────────


@pytest.mark.asyncio
async def test_user_b_cannot_read_user_a_scenario(session_factory):
    seeds = await _seed_users_and_account(session_factory)
    app_a = _make_app(session_factory, _resolver_for("alice@acme.io"))
    app_b = _make_app(session_factory, _resolver_for("bob@acme.io"))
    with TestClient(app_a) as ca:
        created = ca.post(
            "/api/v1/scenarios", json=_trip_payload(seeds["account_id"])
        ).json()
    with TestClient(app_b) as cb:
        # Get one: 404.
        assert cb.get(f"/api/v1/scenarios/{created['id']}").status_code == 404
        # List for Bob: empty (Alice owns the only row).
        assert cb.get("/api/v1/scenarios").json() == []
        # Patch / delete / simulate also 404 for Bob.
        assert cb.patch(
            f"/api/v1/scenarios/{created['id']}",
            json={"name": "stolen"},
        ).status_code == 404
        assert cb.delete(f"/api/v1/scenarios/{created['id']}").status_code == 404
        assert cb.post(
            f"/api/v1/scenarios/{created['id']}/simulate"
        ).status_code == 404
