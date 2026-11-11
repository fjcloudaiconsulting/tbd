"""A ``ValidationError`` may carry a machine-readable ``code`` (TBD-755).

The 400 body gains ``code`` only when the service set one; every existing 400
stays exactly ``{"detail": ...}``.
"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.deps import get_current_user, get_db
from app.main import app
from app.models import User
from app.models.transaction import TransactionStatus
from app.services.exceptions import ValidationError
from tests.services.test_matched_row_actions import (  # noqa: F401  (db_session fixture)
    _create,
    _seed,
    db_session,
)


async def _coded():
    raise ValidationError("x", code="y")


async def _plain():
    raise ValidationError("x")


@pytest.fixture
def probe():
    app.add_api_route("/__t755/coded", _coded)
    app.add_api_route("/__t755/plain", _plain)
    yield
    del app.router.routes[-2:]


async def test_validation_error_code_is_surfaced(probe):
    """#17. Kills: the handler dropping ``code``, or emitting ``"code": null``
    on every 400."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        coded = await c.get("/__t755/coded")
        plain = await c.get("/__t755/plain")
    assert coded.status_code == plain.status_code == 400
    assert coded.json() == {"detail": "x", "code": "y"}
    assert plain.json() == {"detail": "x"}


async def test_reconcile_refusal_carries_code_over_http(db_session):
    """#18. Kills: a code raised in the service but not wired through the
    production app."""
    seed = await _seed(db_session)
    target = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="107.00",
        label="pending", status=TransactionStatus.PENDING, tx_date=date(2026, 6, 1),
    )
    bank = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="365.00",
        label="bank", in_batch=True, tx_date=date(2026, 5, 29),
    )
    user = await db_session.scalar(select(User).where(User.org_id == seed["org_id"]))

    async def _db():
        return db_session

    async def _user():
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/import/{seed['batch_id']}/reconcile",
                json={"transitions": [{
                    "transaction_id": bank.id, "to_state": "matched",
                    "match_with_transaction_id": target.id,
                }]},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "match_target_pending"
