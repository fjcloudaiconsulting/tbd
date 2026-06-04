"""Task 2 — unit tests for ai_status_service + a 403 (no-auth) smoke test for the endpoint.

Service tests use monkeypatch to avoid DB/routing round-trips.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.ai_status import router as ai_status_router
from app.services import ai_status_service


@pytest.mark.asyncio
async def test_get_ai_feature_status_shape(monkeypatch):
    async def fake_features(db, org_id):
        return {"ai.autocategorize": True, "ai.forecast": True, "ai.budget": False}

    async def fake_routing(db, *, org_id, feature_name):
        return (1, "claude-x") if feature_name == "smart_forecast" else None

    monkeypatch.setattr(ai_status_service.feature_service, "get_features", fake_features)
    monkeypatch.setattr(
        ai_status_service.ai_routing_service, "get_routing_for_feature", fake_routing
    )
    out = await ai_status_service.get_ai_feature_status(None, org_id=1)
    assert out["categorize"] == {"entitled": True, "configured": False}
    assert out["forecast"] == {"entitled": True, "configured": True}
    assert out["budget"] == {"entitled": False, "configured": False}


def test_endpoint_requires_auth():
    """GET /api/v1/ai/status must return 403 (no credentials supplied)."""
    app = FastAPI()
    app.include_router(ai_status_router)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/ai/status")
    # HTTPBearer returns 403 when the Authorization header is absent.
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_not_entitled_skips_routing_lookup(monkeypatch):
    """Un-entitled features must never trigger a routing lookup (cost guard)."""
    routing_called: list[str] = []

    async def fake_features(db, org_id):
        return {"ai.autocategorize": False, "ai.forecast": False, "ai.budget": False}

    async def fake_routing(db, *, org_id, feature_name):
        routing_called.append(feature_name)
        return None

    monkeypatch.setattr(ai_status_service.feature_service, "get_features", fake_features)
    monkeypatch.setattr(
        ai_status_service.ai_routing_service, "get_routing_for_feature", fake_routing
    )
    out = await ai_status_service.get_ai_feature_status(None, org_id=1)
    assert routing_called == [], "routing should not be called for un-entitled features"
    for state in out.values():
        assert state == {"entitled": False, "configured": False}
