"""Router tests for LAI.1 /api/v1/ai/categorize.

Pins:
- 200 happy path returns the suggested category + confidence + reasoning.
- 403 when ``ai.autocategorize`` feature gate is closed.
- 404 when the transaction is in a different org.
- 409 when the org has no type-compatible categories.
- 402 when the AI hard cap is exhausted (``AICapExceeded``).
- 412 when no routing is configured, native capability is missing,
  or the routed credential lacks a required capability.
- 502 when the structured-output retry budget is exhausted
  (``StructuredOutputError``) or any other dispatch failure
  (``AIDispatchFailed``).
- Audit event ``ai.categorize.suggested`` written ONLY on success;
  zero audit rows on every failure path.
- Defensive guard: duplicate slugs in the catalog are deduped to the
  lowest-id row.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.category import Category, CategoryType
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.ai_categorize import router as ai_categorize_router
from app.security import hash_password
from app.services.ai_credential_crypto import encrypt
from app.services.ai_providers.base import LLMResponse


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
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch):
    """Redis disabled in tests; the dispatch soft-cap path tolerates None."""
    monkeypatch.setattr(
        "app.services.ai_dispatch.redis_client.get_client",
        lambda: None,
    )


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(ai_categorize_router)
    return app


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    feature_enabled: bool = True,
    with_routing: bool = True,
    extra_org: bool = False,
):
    """Set up an org with a feature plan, a transaction, and routing.

    Returns a dict with the user id + transaction id + categories.
    """
    async with factory() as db:
        # Plan with ai.autocategorize toggled per the test's request.
        plan = Plan(
            slug="ai-tier",
            name="AI Tier",
            features={
                "ai.budget": False,
                "ai.forecast": False,
                "ai.smart_plan": False,
                "ai.autocategorize": feature_enabled,
            },
        )
        db.add(plan)
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        sub = Subscription(
            org_id=org.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        db.add(sub)
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

        # Account
        from app.models.account import Account, AccountType
        acct_type = AccountType(
            org_id=org.id, slug="checking", name="Checking",
        )
        db.add(acct_type)
        await db.commit()
        acct = Account(
            org_id=org.id,
            account_type_id=acct_type.id,
            name="Primary",
            currency="USD",
            is_active=True,
        )
        db.add(acct)
        await db.commit()

        # Categories — slug-bearing, expense-typed.
        cat_groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        cat_rent = Category(
            org_id=org.id, name="Rent", slug="rent",
            type=CategoryType.EXPENSE, is_system=False,
        )
        # An income-only category should never be in the suggestion catalog
        # for an EXPENSE transaction.
        cat_paycheck = Category(
            org_id=org.id, name="Paycheck", slug="paycheck",
            type=CategoryType.INCOME, is_system=False,
        )
        db.add_all([cat_groceries, cat_rent, cat_paycheck])
        await db.commit()

        # Transaction (expense, placed under Rent to satisfy NOT NULL FK;
        # tests assert the AI suggests a different slug).
        tx = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_rent.id,
            description="Whole Foods Market #122",
            amount=Decimal("48.27"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=date.today(),
            settled_date=date.today(),
        )
        db.add(tx)
        await db.commit()

        cred_id = None
        if with_routing:
            cred = OrgAICredential(
                org_id=org.id,
                provider=AiProvider.OPENAI,
                encrypted_api_key=encrypt("sk-test-12345"),
                encrypted_bearer_token=None,
                base_url=None,
                key_fingerprint="0123456789abcdef",
                last_four="2345",
                label="primary",
                discovered_capabilities=[
                    "chat", "embed", "structured_output", "function_call", "stream",
                ],
            )
            db.add(cred)
            await db.commit()
            cred_id = cred.id
            routing = OrgAIDefaultRouting(
                org_id=org.id, credential_id=cred.id, model="gpt-4o-mini"
            )
            db.add(routing)
            await db.commit()

        other_tx_id = None
        if extra_org:
            other_org = Organization(name="Other", billing_cycle_day=1)
            db.add(other_org)
            await db.commit()
            other_acct_type = AccountType(
                org_id=other_org.id, slug="checking", name="Checking",
            )
            db.add(other_acct_type)
            await db.commit()
            other_acct = Account(
                org_id=other_org.id,
                account_type_id=other_acct_type.id,
                name="Other Primary",
                currency="USD",
                is_active=True,
            )
            db.add(other_acct)
            other_cat = Category(
                org_id=other_org.id, name="Misc", slug="misc",
                type=CategoryType.EXPENSE, is_system=False,
            )
            db.add(other_cat)
            await db.commit()
            other_tx = Transaction(
                org_id=other_org.id,
                account_id=other_acct.id,
                category_id=other_cat.id,
                description="cross-org row",
                amount=Decimal("10.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=date.today(),
                settled_date=date.today(),
            )
            db.add(other_tx)
            await db.commit()
            other_tx_id = other_tx.id

        return {
            "owner_id": owner.id,
            "org_id": org.id,
            "tx_id": tx.id,
            "groceries_id": cat_groceries.id,
            "rent_id": cat_rent.id,
            "paycheck_id": cat_paycheck.id,
            "credential_id": cred_id,
            "other_tx_id": other_tx_id,
        }


async def _get_user(factory, user_id: int) -> User:
    async with factory() as db:
        return (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one()


def _adapter_returning(content: str, prompt_tokens: int = 50, completion_tokens: int = 25):
    adapter = MagicMock()
    adapter.chat_structured = AsyncMock(
        return_value=LLMResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model="gpt-4o-mini",
        )
    )
    return adapter


# --------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_happy_path_returns_suggestion(session_factory):
    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    adapter = _adapter_returning(
        '{"category_slug": "groceries", "confidence": 0.92, '
        '"reasoning": "Whole Foods is a grocery store."}'
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transaction_id"] == seeded["tx_id"]
    assert body["category_id"] == seeded["groceries_id"]
    assert body["category_name"] == "Groceries"
    assert body["confidence"] == pytest.approx(0.92)
    assert "Whole Foods" in body["reasoning"] or "grocery" in body["reasoning"]
    adapter.chat_structured.assert_awaited_once()

    # Audit event was written.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "ai.categorize.suggested"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["suggested_category_slug"] == "groceries"
    assert rows[0].detail["transaction_id"] == seeded["tx_id"]


@pytest.mark.asyncio
async def test_feature_gate_closed_returns_403(session_factory):
    seeded = await _seed(session_factory, feature_enabled=False)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    # Patch get_adapter anyway so a missing-routing 412 cannot be confused
    # with the gate-403 we want to assert.
    with patch(
        "app.services.ai_dispatch.get_adapter",
        return_value=_adapter_returning('{"category_slug":"x"}'),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "feature_not_enabled"


@pytest.mark.asyncio
async def test_cross_org_transaction_returns_404(session_factory):
    seeded = await _seed(session_factory, extra_org=True)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/ai/categorize",
        json={"transaction_id": seeded["other_tx_id"]},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "transaction_not_found"


@pytest.mark.asyncio
async def test_no_routing_returns_412(session_factory):
    seeded = await _seed(session_factory, with_routing=False)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/ai/categorize",
        json={"transaction_id": seeded["tx_id"]},
    )
    assert resp.status_code == 412
    assert resp.json()["detail"]["code"] == "ai_routing_not_configured"


@pytest.mark.asyncio
async def test_unknown_slug_returns_502(session_factory):
    """Service-layer defense-in-depth: an unknown slug returns 502."""
    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))

    from app.services.ai_dispatch import StructuredDispatchResult
    from app.services.ai_providers.base import StructuredResponse

    fake_result = StructuredDispatchResult(
        response=StructuredResponse(
            parsed={
                "category_slug": "made-up-slug",
                "confidence": 0.9,
                "reasoning": "n/a",
            },
            raw_text="...",
            prompt_tokens=10,
            completion_tokens=5,
            model="gpt-4o-mini",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.ai_categorize_service.call_llm_structured",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"].startswith("suggestion_rejected")


@pytest.mark.asyncio
async def test_no_compatible_categories_returns_409(session_factory):
    """An org with zero type-compatible categories cannot be served."""
    seeded = await _seed(session_factory)

    # Strip expense categories so the catalog is empty for the expense
    # transaction. Re-point the seeded tx at the income category before
    # delete to dodge the NOT NULL FK.
    async with session_factory() as db:
        tx = (
            await db.execute(
                select(Transaction).where(Transaction.id == seeded["tx_id"])
            )
        ).scalar_one()
        tx.category_id = seeded["paycheck_id"]
        await db.commit()
        rows = (
            await db.execute(
                select(Category).where(
                    Category.id.in_(
                        [seeded["groceries_id"], seeded["rent_id"]]
                    )
                )
            )
        ).scalars().all()
        for r in rows:
            await db.delete(r)
        await db.commit()

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/ai/categorize",
        json={"transaction_id": seeded["tx_id"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "category_catalog_empty"


# --- dispatch-error path coverage ----------------------------------------
#
# These tests patch ``ai_categorize_service.call_llm_structured`` directly
# so each typed dispatch failure can be exercised without driving a full
# adapter mock. Each one ALSO asserts the audit table is empty after the
# failure — the "audit on success only" policy is otherwise unpinned on
# the failure side and easy to regress.


async def _assert_no_audit_rows(session_factory) -> None:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "ai.categorize.suggested"
                )
            )
        ).scalars().all()
    assert rows == [], (
        f"failure path must NOT emit an ai.categorize.suggested audit row; "
        f"found {len(rows)}"
    )


@pytest.mark.asyncio
async def test_cap_exceeded_returns_402(session_factory):
    """Hard-cap exhaustion → 402 ``ai_hard_cap_exceeded``, no audit row."""
    from app.services.ai_dispatch import AICapExceeded

    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    with patch(
        "app.services.ai_categorize_service.call_llm_structured",
        new=AsyncMock(side_effect=AICapExceeded()),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "ai_hard_cap_exceeded"
    await _assert_no_audit_rows(session_factory)


@pytest.mark.asyncio
async def test_native_not_available_returns_412(session_factory):
    """Provider lacks structured-output capability → 412
    ``ai_native_not_available``, no audit row."""
    from app.services.ai_providers.base import NativeNotAvailable

    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    with patch(
        "app.services.ai_categorize_service.call_llm_structured",
        new=AsyncMock(side_effect=NativeNotAvailable()),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 412
    assert resp.json()["detail"]["code"] == "ai_native_not_available"
    await _assert_no_audit_rows(session_factory)


@pytest.mark.asyncio
async def test_capability_not_supported_returns_412(session_factory):
    """Routed credential lacks a required capability → 412 with
    structured detail (code + capability + feature_key)."""
    from app.services.ai_dispatch import AICapabilityNotSupported

    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    with patch(
        "app.services.ai_categorize_service.call_llm_structured",
        new=AsyncMock(
            side_effect=AICapabilityNotSupported(
                capability="structured_output",
                feature_key="categorize_transactions",
            )
        ),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 412
    detail = resp.json()["detail"]
    assert detail["code"] == "ai_capability_not_supported"
    assert detail["capability"] == "structured_output"
    assert detail["feature_key"] == "categorize_transactions"
    await _assert_no_audit_rows(session_factory)


@pytest.mark.asyncio
async def test_structured_output_exhausted_returns_502(session_factory):
    """Structured-output retry budget exhausted → 502
    ``ai_structured_output_failed``, no audit row. Pins the
    StructuredOutputError → 502 mapping the router promises."""
    from app.services.ai_providers.base import StructuredOutputError

    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    with patch(
        "app.services.ai_categorize_service.call_llm_structured",
        new=AsyncMock(side_effect=StructuredOutputError("retries_exhausted")),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "ai_structured_output_failed"
    await _assert_no_audit_rows(session_factory)


@pytest.mark.asyncio
async def test_dispatch_failed_returns_502(session_factory):
    """Adapter/provider transport error → 502 with the dispatcher's
    own error code propagated to the response body."""
    from app.services.ai_dispatch import AIDispatchFailed

    seeded = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    with patch(
        "app.services.ai_categorize_service.call_llm_structured",
        new=AsyncMock(side_effect=AIDispatchFailed("connection_error")),
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "connection_error"
    await _assert_no_audit_rows(session_factory)


@pytest.mark.asyncio
async def test_duplicate_slug_is_deduped_lowest_id_wins(session_factory):
    """If two categories in the same org share a slug, the service
    deterministically keeps the lowest-id row. The dropped row is
    invisible to the LLM (not in the enum) — pin this so the
    defensive guard doesn't silently regress to a non-deterministic
    dict insert order.
    """
    seeded = await _seed(session_factory)

    # Add a second expense category with the same slug as Groceries.
    # Higher id wins on insert order; the defensive guard should drop
    # this one in favor of the original Groceries row.
    async with session_factory() as db:
        dup = Category(
            org_id=seeded["org_id"],
            name="Groceries Duplicate",
            slug="groceries",
            type=CategoryType.EXPENSE,
            is_system=False,
        )
        db.add(dup)
        await db.commit()
        dup_id = dup.id

    async def resolver(_factory):
        return await _get_user(session_factory, seeded["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    adapter = _adapter_returning(
        '{"category_slug": "groceries", "confidence": 0.9, '
        '"reasoning": "Match the lowest-id row."}'
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        resp = client.post(
            "/api/v1/ai/categorize",
            json={"transaction_id": seeded["tx_id"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The original Groceries row (lower id) must win, never the dup.
    assert body["category_id"] == seeded["groceries_id"]
    assert body["category_id"] != dup_id
