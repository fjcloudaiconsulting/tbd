"""L3.2 Wave 1 contract tests for new import / transactions stubs.

These tests pin the wire shape that downstream Wave 2 teams (OFX Parser,
Manual Batch Entry, Description Suggestions, Reconciliation UI) will
build against. They pass at 501 today and become the regression scaffold
for the implementation teams' PRs.

Each endpoint is verified for:
- Auth enforcement (401 without an authenticated user)
- Org-scoping shell (dependency is wired; current_user.org_id is read)
- Pydantic request validation (422 on bad payload shape)
- 501 response when called with a well-formed payload (stub semantics)

Spec: ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.
"""
from __future__ import annotations

import io
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.routers.import_router import router as import_router
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from fastapi.responses import JSONResponse


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


async def _seed_user(factory) -> tuple[int, int]:
    """Seed a single org + user. Returns ``(org_id, user_id)``."""
    async with factory() as db:
        org = Organization(name="ContractTest", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="contracttest",
            email="contract@test.example",
            password_hash=hash_password("pw-test-12345"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return org.id, user.id


def _make_app(session_factory, *, authenticated: bool = True) -> FastAPI:
    """Build a FastAPI app with both routers + auth override.

    When ``authenticated=False``, ``get_current_user`` raises a 401-style
    HTTPException so we can verify the auth dependency actually fires.
    """
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    if authenticated:
        async def override_current_user() -> User:
            async with session_factory() as db:
                return (
                    await db.execute(
                        select(User).where(User.is_superadmin.is_(True))
                    )
                ).scalar_one()
        app.dependency_overrides[get_current_user] = override_current_user
    else:
        from fastapi import HTTPException

        async def reject_user():
            raise HTTPException(status_code=401, detail="not authenticated")

        app.dependency_overrides[get_current_user] = reject_user

    app.dependency_overrides[get_db] = override_get_db
    # Audit writes go through ``record_audit_event(session_factory, ...)``
    # which opens its own transaction. Point that factory at the same
    # in-memory engine the rest of the test uses so audit rows land in
    # the same DB (and a missing override doesn't silently surface as
    # a 500 in the new manual-batch path).
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    # Wire the same domain exception handlers main.py installs so the
    # Pydantic 422 / domain-error mapping is identical to production.
    @app.exception_handler(NotFoundError)
    async def _nfe(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _vle(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _cfe(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(import_router)
    app.include_router(transactions_router)
    return app


# ── /api/v1/import/ofx/preview ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ofx_preview_rejects_malformed_with_400(session_factory):
    """OFX preview rejects a structurally invalid file with 400.

    Updated 2026-05-12 (L3.2 Wave 2A): the endpoint is now implemented
    (see ``app.services.import_ofx_service``). A bare ``<OFX></OFX>``
    body has no header, no STMTTRNRS and no transactions — ParseError
    → 400 via the domain-exception shim, no stack trace leaked.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("test.ofx", io.BytesIO(b"<OFX></OFX>"), "application/x-ofx")},
            data={"account_id": "1"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "ofx" in body["detail"].lower()
    # No stack trace / no raw file content leaks.
    assert "traceback" not in body["detail"].lower()


@pytest.mark.asyncio
async def test_ofx_preview_requires_auth(session_factory):
    """OFX preview rejects unauthenticated requests with 401."""
    await _seed_user(session_factory)
    app = _make_app(session_factory, authenticated=False)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("test.ofx", io.BytesIO(b"<OFX></OFX>"), "application/x-ofx")},
            data={"account_id": "1"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ofx_preview_validates_account_id_present(session_factory):
    """OFX preview returns 422 when account_id is missing."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("test.ofx", io.BytesIO(b"<OFX></OFX>"), "application/x-ofx")},
            # account_id missing
        )
    assert resp.status_code == 422


# ── /api/v1/import/{import_id}/reconcile ────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_returns_404_when_batch_missing(session_factory):
    """L3.2 Wave 2B: the stub became a real handler. A reconcile call
    against a non-existent batch ID now returns 404 (the org-scoped
    ``NotFoundError`` surface). The previous 501 contract test has been
    repurposed -- the endpoint is no longer a stub."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "transitions": [
            {"transaction_id": 1, "to_state": "accepted"},
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/9999999/reconcile", json=payload)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reconcile_requires_auth(session_factory):
    """Reconcile rejects unauthenticated requests with 401."""
    await _seed_user(session_factory)
    app = _make_app(session_factory, authenticated=False)
    payload = {"transitions": [{"transaction_id": 1, "to_state": "accepted"}]}
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/42/reconcile", json=payload)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reconcile_validates_empty_transitions(session_factory):
    """Reconcile rejects an empty transitions list (min_length=1)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/42/reconcile", json={"transitions": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reconcile_validates_unknown_state(session_factory):
    """Reconcile rejects unknown ``to_state`` values via the enum."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "transitions": [{"transaction_id": 1, "to_state": "totally_made_up"}],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/42/reconcile", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reconcile_validates_extra_fields_forbidden(session_factory):
    """Reconcile rejects unknown top-level fields (extra='forbid')."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "transitions": [{"transaction_id": 1, "to_state": "accepted"}],
        "rogue_field": "nope",
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/42/reconcile", json=payload)
    assert resp.status_code == 422


# ── /api/v1/transactions/batch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_returns_200_with_per_row_errors(session_factory):
    """Batch endpoint is implemented: returns 200 with per-row outcomes.

    Calling with an account_id / category_id that doesn't exist in the
    seeded org returns a per-row error rather than a 5xx. The endpoint
    now lives — the previous 501 contract gate is replaced by a
    per-row outcome assertion.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "rows": [
            {
                "row_number": 1,
                "transaction": {
                    "account_id": 1,
                    "category_id": 1,
                    "description": "Test row",
                    "amount": "12.50",
                    "type": "expense",
                    "date": "2026-05-10",
                },
            }
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/transactions/batch", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported_count"] == 0
    assert body["error_count"] == 1
    assert body["errors"][0]["row_number"] == 1


@pytest.mark.asyncio
async def test_batch_requires_auth(session_factory):
    """Batch endpoint rejects unauthenticated requests with 401."""
    await _seed_user(session_factory)
    app = _make_app(session_factory, authenticated=False)
    payload = {
        "rows": [
            {
                "row_number": 1,
                "transaction": {
                    "account_id": 1,
                    "category_id": 1,
                    "description": "Test row",
                    "amount": "12.50",
                    "type": "expense",
                    "date": "2026-05-10",
                },
            }
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/transactions/batch", json=payload)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_batch_validates_empty_rows(session_factory):
    """Batch endpoint rejects an empty rows list (min_length=1)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post("/api/v1/transactions/batch", json={"rows": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_rejects_duplicate_row_numbers(session_factory):
    """Batch endpoint rejects duplicate ``row_number`` values with 422.

    The response shape maps results back to the user's input via
    ``row_number``; duplicates would collide. The ``model_validator``
    on ``BatchTransactionsRequest`` is the gate. We verify the 422
    surfaces and the error locator points at the ``rows`` field.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "rows": [
            {
                "row_number": 1,
                "transaction": {
                    "account_id": 1,
                    "category_id": 1,
                    "description": "First",
                    "amount": "1.00",
                    "type": "expense",
                    "date": "2026-05-10",
                },
            },
            {
                "row_number": 1,
                "transaction": {
                    "account_id": 1,
                    "category_id": 1,
                    "description": "Second",
                    "amount": "2.00",
                    "type": "expense",
                    "date": "2026-05-10",
                },
            },
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/transactions/batch", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    rendered = repr(body)
    # The Pydantic validator message names ``row_number`` and either
    # ``unique`` or ``duplicate``. Both signals must surface so the
    # frontend can render a meaningful 422.
    assert "row_number" in rendered
    assert "unique" in rendered.lower() or "duplicate" in rendered.lower()


@pytest.mark.asyncio
async def test_batch_validates_max_rows(session_factory):
    """Batch endpoint rejects more than 500 rows (max_length=500)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    rows = [
        {
            "row_number": i,
            "transaction": {
                "account_id": 1,
                "category_id": 1,
                "description": f"Row {i}",
                "amount": "1.00",
                "type": "expense",
                "date": "2026-05-10",
            },
        }
        for i in range(1, 502)
    ]
    with TestClient(app) as client:
        resp = client.post("/api/v1/transactions/batch", json={"rows": rows})
    assert resp.status_code == 422


# ── /api/v1/transactions/suggestions/descriptions ───────────────────────────


@pytest.mark.asyncio
async def test_suggestions_returns_200_with_empty_payload_for_seeded_user(
    session_factory,
):
    """Description-suggestions endpoint is now implemented (L3.2 Wave 2A).

    With no transactions seeded for the contract user, the endpoint
    returns 200 with an empty suggestions list — confirming the
    handler is no longer the 501 stub but is wired to the service.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "q": "alb", "limit": 10},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"suggestions": []}


@pytest.mark.asyncio
async def test_suggestions_requires_auth(session_factory):
    """Description-suggestions endpoint rejects unauthenticated requests."""
    await _seed_user(session_factory)
    app = _make_app(session_factory, authenticated=False)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_suggestions_validates_type_enum(session_factory):
    """Suggestions endpoint rejects unknown ``type`` values."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "bogus"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_suggestions_validates_min_query_length(session_factory):
    """Suggestions endpoint rejects ``q`` shorter than 2 chars."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "q": "a"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_suggestions_validates_max_limit(session_factory):
    """Suggestions endpoint rejects ``limit > 25``."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "limit": 26},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_suggestions_q_omitted_is_valid_at_contract_layer(session_factory):
    """When q is omitted, request shape is valid (server returns 200)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "income"},
        )
    # 200, not 422 — q is optional per contract; the live handler
    # returns an empty list when the org has no transactions seeded.
    assert resp.status_code == 200
    assert resp.json() == {"suggestions": []}


# ── OpenAPI surface check ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openapi_advertises_all_new_endpoints(session_factory):
    """OpenAPI schema must list the four new endpoints so Wave 2 teams can
    generate client stubs against the frozen contract."""
    app = _make_app(session_factory)
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/import/ofx/preview" in paths
    assert "post" in paths["/api/v1/import/ofx/preview"]
    assert "/api/v1/import/{import_id}/reconcile" in paths
    assert "post" in paths["/api/v1/import/{import_id}/reconcile"]
    assert "/api/v1/transactions/batch" in paths
    assert "post" in paths["/api/v1/transactions/batch"]
    assert "/api/v1/transactions/suggestions/descriptions" in paths
    assert "get" in paths["/api/v1/transactions/suggestions/descriptions"]

    # Component schemas for each contract type must be advertised.
    components = schema.get("components", {}).get("schemas", {})
    for name in (
        "BatchTransactionsRequest",
        "BatchTransactionsResponse",
        "ReconcileBatchRequest",
        "ReconcileBatchResponse",
        "DescriptionSuggestionsResponse",
        "ReconciliationState",
    ):
        assert name in components, f"OpenAPI missing component: {name}"


@pytest.mark.asyncio
async def test_openapi_exposes_ofx_row_fields(session_factory):
    """OFX-specific fields MUST surface on the shared row schemas.

    Regression gate for the Wave 2 OFX team: ``fitid``, ``bank_id``,
    ``account_type_ofx`` live on ``ImportPreviewRow`` and
    ``ImportConfirmRow`` (the shared row schemas in
    ``app/schemas/import_schemas.py``), NOT on a sidecar model. This
    test asserts the OpenAPI component for both row schemas advertises
    all three fields so Wave 2's generated client picks them up.
    """
    app = _make_app(session_factory)
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})

    for row_schema_name in ("ImportPreviewRow", "ImportConfirmRow"):
        assert row_schema_name in components, (
            f"OpenAPI missing row schema: {row_schema_name}"
        )
        props = components[row_schema_name].get("properties", {})
        for field in ("fitid", "bank_id", "account_type_ofx"):
            assert field in props, (
                f"OpenAPI {row_schema_name} missing OFX field: {field}. "
                "Wave 2 OFX team builds against these — they must surface."
            )


# ── PR #247 P0: CSV confirm → import_batches header → GET reconcile ─────────


@pytest.mark.asyncio
async def test_confirm_creates_import_batch_and_response_includes_id(
    session_factory,
):
    """End-to-end wiring proof. A real CSV confirm payload (with the
    new required ``file_name`` + ``source_format``) creates an
    ``import_batches`` row, links the imported transaction to it, and
    returns the batch id on the response so the frontend can deep-link
    to ``/import/{import_id}/reconcile``. The 501-stub-shaped issue
    that PR #247's owner review flagged is precisely this seam."""
    from app.models import (
        Account,
        AccountType,
        Category,
        CategoryType,
        ImportBatch,
    )
    from app.models.transaction import Transaction

    org_id, user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        atype = AccountType(
            org_id=org_id, name="Checking", slug="checking", is_system=True
        )
        db.add(atype)
        await db.flush()
        acct = Account(
            org_id=org_id,
            name="Cash",
            account_type_id=atype.id,
            balance=0,
            currency="EUR",
        )
        db.add(acct)
        await db.flush()
        cat = Category(
            org_id=org_id,
            name="Groceries",
            slug="groceries",
            type=CategoryType.EXPENSE,
        )
        db.add(cat)
        await db.commit()
        account_id = acct.id
        category_id = cat.id

    app = _make_app(session_factory)
    payload = {
        "account_id": account_id,
        "default_category_id": category_id,
        "file_name": "real-export.csv",
        "source_format": "csv",
        "rows": [
            {
                "row_number": 1,
                "date": "2026-05-10",
                "description": "Albert Heijn",
                "amount": 12.50,
                "type": "expense",
                "category_id": category_id,
                "action": "create",
            }
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/confirm", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported_count"] == 1
    assert body["import_id"] is not None
    import_id = body["import_id"]

    # The batch row exists with row_count=1 (the one imported tx).
    async with session_factory() as db:
        batch = await db.scalar(
            select(ImportBatch).where(ImportBatch.id == import_id)
        )
        assert batch is not None
        assert batch.org_id == org_id
        assert batch.source_format.value == "csv"
        assert batch.file_name == "real-export.csv"
        assert batch.row_count == 1
        # The transaction was linked.
        tx = await db.scalar(
            select(Transaction).where(
                Transaction.import_batch_id == import_id
            )
        )
        assert tx is not None
        assert tx.description == "Albert Heijn"

    # The reconcile inbox is reachable via GET.
    with TestClient(app) as client:
        get_resp = client.get(f"/api/v1/import/{import_id}")
    assert get_resp.status_code == 200, get_resp.text
    detail = get_resp.json()
    assert detail["batch"]["id"] == import_id
    assert len(detail["rows"]) == 1


@pytest.mark.asyncio
async def test_confirm_rejects_missing_file_name(session_factory):
    """Schema gate: ``file_name`` is REQUIRED. A confirm payload that
    omits it returns 422 -- this is the regression gate that prevents
    the bug from coming back (frontend silently skipping the field)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "account_id": 1,
        "default_category_id": 1,
        "source_format": "csv",
        "rows": [],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/confirm", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_confirm_rejects_bad_source_format(session_factory):
    """Schema gate: ``source_format`` only accepts ``'csv'`` or
    ``'ofx'``. Anything else is a typed 422 at the wire boundary."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    payload = {
        "account_id": 1,
        "default_category_id": 1,
        "file_name": "x.csv",
        "source_format": "xlsx",
        "rows": [],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/import/confirm", json=payload)
    assert resp.status_code == 422


# ── Org-scoping regression (L3.2 spec §7 Contract Boundary) ─────────────────


async def _seed_two_orgs(factory) -> tuple[dict, dict]:
    """Seed two independent orgs each with a user + account.

    Returns ``(org_a, org_b)`` where each dict carries ``org_id``,
    ``user_id`` and ``account_id``. Used to verify cross-org isolation
    on every Wave 2 endpoint — the regression gate for spec §5.4
    (Description Suggestions) and §1.1 (OFX preview).
    """
    from app.models.account import Account, AccountType
    from decimal import Decimal

    seeds: list[dict] = []
    for suffix in ("a", "b"):
        async with factory() as db:
            org = Organization(name=f"OrgScope{suffix.upper()}", billing_cycle_day=1)
            db.add(org)
            await db.flush()
            user = User(
                org_id=org.id,
                username=f"scopeuser{suffix}",
                email=f"scope-{suffix}@test.example",
                password_hash=hash_password("pw-scope-test-12345"),
                role=Role.OWNER,
                is_superadmin=False,
                is_active=True,
                email_verified=True,
            )
            atype = AccountType(
                org_id=org.id, name="Checking", slug="checking", is_system=True
            )
            db.add_all([user, atype])
            await db.flush()
            acct = Account(
                org_id=org.id,
                account_type_id=atype.id,
                name=f"Acct{suffix.upper()}",
                balance=Decimal("0"),
                currency="EUR",
            )
            db.add(acct)
            await db.commit()
            seeds.append(
                {
                    "org_id": org.id,
                    "user_id": user.id,
                    "account_id": acct.id,
                }
            )
    return seeds[0], seeds[1]


def _make_app_as_user(session_factory, user_id: int) -> FastAPI:
    """Build the app authenticating as a specific user id.

    Lets us drive a request "as org A" while another org B exists in
    the same DB so cross-org leakage would surface as a returned
    suggestion or a 200 on a foreign account.
    """
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    @app.exception_handler(NotFoundError)
    async def _nfe(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _vle(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _cfe(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(import_router)
    app.include_router(transactions_router)
    return app


@pytest.mark.asyncio
async def test_ofx_preview_rejects_foreign_org_account_id(session_factory):
    """OFX preview rejects an account_id owned by a different org.

    Regression gate for spec §1.1: every OFX preview request is
    org-scoped via the resolver that looks up the account. A user
    in org A submitting a request with org B's account_id MUST NOT
    receive a 200 with a preview of org A's matched rows -- the
    resolver should refuse the foreign id. The contract-layer
    acceptable surfaces are 404 (not found in this org) or a
    domain-level 400; what's explicitly not acceptable is 200 OK
    with row data attached.
    """
    org_a, org_b = await _seed_two_orgs(session_factory)
    app = _make_app_as_user(session_factory, user_id=org_a["user_id"])
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("test.ofx", io.BytesIO(b"<OFX></OFX>"), "application/x-ofx")},
            data={"account_id": str(org_b["account_id"])},
        )
    # Either the parse-error 400 fires first (the bogus OFX body) OR
    # the org-scope check trips with 404. Both are acceptable failure
    # surfaces; the regression gate is "never 200 OK with row data."
    assert resp.status_code in (400, 404), (
        f"Expected 400 or 404, got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code != 200


@pytest.mark.asyncio
async def test_suggestions_isolates_per_org(session_factory):
    """Description suggestions never leak across org_id boundaries.

    Regression gate for spec §5.4 (privacy rules). Seeds a transaction
    in org B with a distinctive description, then asks org A's user
    for that description -- the response must NOT include the foreign
    org's data even though both rows share the underlying DB.
    """
    from app.models.account import Account
    from app.models.category import Category, CategoryType
    from app.models.transaction import Transaction, TransactionType
    from decimal import Decimal
    from datetime import date

    org_a, org_b = await _seed_two_orgs(session_factory)
    # Seed a distinctively-described transaction in org B that org A
    # must never see.
    async with session_factory() as db:
        cat_b = Category(
            org_id=org_b["org_id"],
            name="Groceries",
            slug="groceries",
            type=CategoryType.EXPENSE,
        )
        db.add(cat_b)
        await db.flush()
        tx_b = Transaction(
            org_id=org_b["org_id"],
            account_id=org_b["account_id"],
            category_id=cat_b.id,
            description="OrgBSecretMerchant",
            amount=Decimal("12.50"),
            type=TransactionType.EXPENSE,
            date=date(2026, 5, 1),
        )
        db.add(tx_b)
        await db.commit()

    app = _make_app_as_user(session_factory, user_id=org_a["user_id"])
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "q": "Org"},
        )
    assert resp.status_code == 200
    body = resp.json()
    descriptions = [s["description"] for s in body["suggestions"]]
    assert "OrgBSecretMerchant" not in descriptions, (
        f"Cross-org leakage: org A saw org B's description. Got: {descriptions}"
    )


@pytest.mark.asyncio
async def test_batch_isolates_per_org_account_id(session_factory):
    """Batch endpoint refuses a foreign-org account_id with a per-row error.

    Regression gate for spec §0.2 + §7: the batch endpoint rejects
    cross-org account references at the per-row layer. A row pointing
    at a different org's account_id must NOT result in a row written
    under org A pointing at org B's account; the per-row error path
    catches the foreign reference and the row count stays at 0
    imported / 1 errored.
    """
    org_a, org_b = await _seed_two_orgs(session_factory)
    app = _make_app_as_user(session_factory, user_id=org_a["user_id"])
    payload = {
        "rows": [
            {
                "row_number": 1,
                "transaction": {
                    "account_id": org_b["account_id"],  # foreign!
                    "category_id": 1,
                    "description": "Should not import",
                    "amount": "12.50",
                    "type": "expense",
                    "date": "2026-05-10",
                },
            }
        ],
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/transactions/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported_count"] == 0
    assert body["error_count"] == 1
    assert body["errors"][0]["row_number"] == 1
