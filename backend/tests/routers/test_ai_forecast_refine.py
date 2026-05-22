"""LAI.2 — Smart Forecast refinement router tests.

Pins:
- Feature gate closed -> 403.
- Feature gate open + no routing -> 200 with ``ai_applied=False`` and
  ``fallback_reason="ai_routing_not_configured"`` (still serves the
  baseline, never 5xx).
- Feature gate open + happy LLM path -> 200 with ``ai_applied=True``,
  multiplier applied to the baseline category, anomalies surfaced.
- Out-of-band LLM response (multiplier=5.0) -> Pydantic rejects ->
  fallback to baseline with ``ai_response_invalid_schema``.
- Cross-org isolation: an org with a closed gate gets 403 even if
  another org has the gate open.
"""
from __future__ import annotations

import base64
import datetime
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.category import Category
from app.models.feature_override import OrgFeatureOverride
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.ai_forecast import router as ai_forecast_router
from app.security import hash_password
from app.services.ai_providers.base import StructuredOutputError


# ---------- fixtures -------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    """The AI dispatch path peeks at the credential encryption key on
    import; even though we mock the dispatch call itself, defaulting
    the setting here keeps the test surface minimal for any future
    code path that touches the encryption module at import time.
    """
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


def _make_app(session_factory, user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await user_resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(ai_forecast_router)
    return app


async def _seed_org_with_data(
    factory: async_sessionmaker[AsyncSession],
    *,
    enable_ai_forecast: bool,
) -> dict:
    """Seed an org with one account, one category, one settled history
    transaction, and one in-period settled transaction. The settled
    history row is what gives the refinement service something to
    feed into the prompt; the in-period one anchors a baseline forecast
    category row that the multiplier can act on.
    """
    today = datetime.date.today()
    period_start = today.replace(day=1)
    # End of current month-ish; the baseline computation walks 30d if
    # the period has no end_date.
    period_end = period_start + datetime.timedelta(days=27)
    history_date = period_start - datetime.timedelta(days=45)

    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()

        if enable_ai_forecast:
            db.add(
                OrgFeatureOverride(
                    org_id=org.id,
                    feature_key="ai.forecast",
                    value=True,
                    set_by=owner.id,
                )
            )
            await db.commit()

        # Billing period: compute_forecast falls back to
        # get_current_period which auto-creates if missing, but we
        # pre-seed an explicit one so the test pins the date math.
        period = BillingPeriod(
            org_id=org.id,
            start_date=period_start,
            end_date=period_end,
        )
        db.add(period)
        await db.flush()

        at = AccountType(
            org_id=org.id,
            name="Checking",
            slug="checking",
            is_system=True,
        )
        db.add(at)
        await db.flush()
        account = Account(
            org_id=org.id,
            name="Main",
            account_type_id=at.id,
            balance=Decimal("1000.00"),
            currency="EUR",
            is_default=True,
        )
        db.add(account)
        category = Category(
            org_id=org.id,
            name="Groceries",
            type="expense",
        )
        db.add(category)
        await db.commit()

        # Settled transaction in the current period -> appears in the
        # baseline categories breakdown so the multiplier has something
        # to act on.
        db.add(
            Transaction(
                org_id=org.id,
                account_id=account.id,
                category_id=category.id,
                date=period_start + datetime.timedelta(days=5),
                settled_date=period_start + datetime.timedelta(days=5),
                amount=Decimal("100.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                description="seed",
            )
        )
        # Historical row from a month ago -> contributes to the
        # 6-month history aggregate the refinement service builds.
        db.add(
            Transaction(
                org_id=org.id,
                account_id=account.id,
                category_id=category.id,
                date=history_date,
                settled_date=history_date,
                amount=Decimal("120.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                description="seed-history",
            )
        )
        await db.commit()

        return {
            "org_id": org.id,
            "owner_id": owner.id,
            "period_start": period_start.isoformat(),
            "category_id": category.id,
        }


async def _get_user(factory, user_id: int) -> User:
    from sqlalchemy import select

    async with factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one()


# ---------- tests ----------------------------------------------------


@pytest.mark.asyncio
async def test_feature_gate_closed_returns_403(session_factory):
    """No override row -> ai.forecast default-false -> 403.

    Pins the defense-in-depth gate. The router MUST refuse before
    calling into the service, so no audit row, no dispatch, no
    baseline computation.
    """
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=False)

    async def resolver(_factory):
        return await _get_user(session_factory, seed["owner_id"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/ai/forecast/refine",
        json={"period_start": seed["period_start"]},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "feature_not_enabled"
    assert body["detail"]["feature_key"] == "ai.forecast"


@pytest.mark.asyncio
async def test_no_routing_falls_back_to_baseline(session_factory):
    """Feature gate open + no AI routing -> baseline with typed reason.

    The service must NOT 5xx when routing is missing; it must return
    the baseline forecast with provenance.ai_applied=False so the UI
    keeps rendering something useful.
    """
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=True)

    async def resolver(_factory):
        return await _get_user(session_factory, seed["owner_id"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/ai/forecast/refine",
        json={"period_start": seed["period_start"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance"]["ai_applied"] is False
    assert body["provenance"]["fallback_reason"] == "ai_routing_not_configured"
    # Baseline still computed correctly.
    assert body["baseline_forecast_expense"] == body["refined_forecast_expense"]
    # The seeded category appears in the response with multiplier=1.0.
    assert any(c["category_id"] == seed["category_id"] for c in body["categories"])


@pytest.mark.asyncio
async def test_happy_path_applies_multiplier(session_factory):
    """Mocked dispatch returns valid adjustments -> multipliers applied.

    Verifies the LLM->Pydantic->baseline-math chain.
    """
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=True)

    async def resolver(_factory):
        return await _get_user(session_factory, seed["owner_id"])

    # Build a fake StructuredResponse-bearing DispatchResult. The
    # ``parsed`` dict matches the schema the service expects from
    # call_llm_structured -> AIForecastAdjustments.
    from app.services.ai_providers.base import StructuredResponse
    from app.services.ai_dispatch import StructuredDispatchResult

    fake_response = StructuredDispatchResult(
        response=StructuredResponse(
            parsed={
                "seasonal": [
                    {
                        "category_id": seed["category_id"],
                        "category_name": "Groceries",
                        "multiplier": 1.2,
                        "rationale": "Historical Nov uptick.",
                    }
                ],
                "anomalies": [
                    {
                        "category_id": seed["category_id"],
                        "category_name": "Groceries",
                        "description": "120 USD month-over-month vs 100 baseline.",
                        "severity": "info",
                    }
                ],
                "confidence": 0.78,
                "summary": "One small seasonal uptick in groceries.",
            },
            raw_text="{}",
            prompt_tokens=10,
            completion_tokens=10,
            model="mock-model",
            retries_used=0,
        ),
        ledger_id=1,
    )

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with patch(
        "app.services.ai_forecast_refine_service.ai_dispatch.call_llm_structured",
        return_value=fake_response,
    ):
        resp = client.post(
            "/api/v1/ai/forecast/refine",
            json={"period_start": seed["period_start"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance"]["ai_applied"] is True
    assert body["provenance"]["confidence"] == 0.78
    assert body["provenance"]["model"] == "mock-model"
    # The seeded category had a baseline forecast of 100.00 (one settled
    # transaction in the period). 1.2x -> 120.00.
    cat = next(c for c in body["categories"] if c["category_id"] == seed["category_id"])
    assert Decimal(cat["baseline_forecast"]) == Decimal("100.00")
    assert Decimal(cat["refined_forecast"]) == Decimal("120.00")
    assert cat["multiplier"] == 1.2
    # Anomaly surfaced.
    assert len(body["anomalies"]) == 1
    assert body["anomalies"][0]["severity"] == "info"


@pytest.mark.asyncio
async def test_out_of_band_multiplier_falls_back_to_baseline(session_factory):
    """LLM returns multiplier=5.0 -> Pydantic rejects -> baseline served.

    This is the primary safety control: a misbehaving model cannot
    blow up a forecast line. Out-of-band response triggers a fallback
    with ``ai_response_invalid_schema``.
    """
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=True)

    async def resolver(_factory):
        return await _get_user(session_factory, seed["owner_id"])

    from app.services.ai_providers.base import StructuredResponse
    from app.services.ai_dispatch import StructuredDispatchResult

    fake_response = StructuredDispatchResult(
        response=StructuredResponse(
            parsed={
                "seasonal": [
                    {
                        "category_id": seed["category_id"],
                        "category_name": "Groceries",
                        "multiplier": 5.0,  # out of [0.5, 1.5]
                        "rationale": "Bad model.",
                    }
                ],
                "anomalies": [],
                "confidence": 0.5,
                "summary": "Bad output.",
            },
            raw_text="{}",
            prompt_tokens=10,
            completion_tokens=10,
            model="mock-model",
            retries_used=0,
        ),
        ledger_id=1,
    )

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with patch(
        "app.services.ai_forecast_refine_service.ai_dispatch.call_llm_structured",
        return_value=fake_response,
    ):
        resp = client.post(
            "/api/v1/ai/forecast/refine",
            json={"period_start": seed["period_start"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance"]["ai_applied"] is False
    assert body["provenance"]["fallback_reason"] == "ai_response_invalid_schema"
    # Refined equals baseline because no multiplier was applied.
    assert body["refined_forecast_expense"] == body["baseline_forecast_expense"]


@pytest.mark.asyncio
async def test_structured_output_exhausted_falls_back(session_factory):
    """Dispatch raises StructuredOutputError -> baseline + typed reason."""
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=True)

    async def resolver(_factory):
        return await _get_user(session_factory, seed["owner_id"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with patch(
        "app.services.ai_forecast_refine_service.ai_dispatch.call_llm_structured",
        side_effect=StructuredOutputError(),
    ):
        resp = client.post(
            "/api/v1/ai/forecast/refine",
            json={"period_start": seed["period_start"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance"]["ai_applied"] is False
    assert body["provenance"]["fallback_reason"] == "ai_structured_output_failed"


@pytest.mark.asyncio
async def test_invalid_period_start_returns_400(session_factory):
    """Malformed period_start string -> 400, not a 500."""
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=True)

    async def resolver(_factory):
        return await _get_user(session_factory, seed["owner_id"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/ai/forecast/refine",
        json={"period_start": "not-a-date"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_period_start"
