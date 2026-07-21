"""Router tests for L4.11 admin feature-override PUT endpoint.

`PUT /api/v1/admin/orgs/{org_id}/feature-overrides/{feature_key}`
upserts a per-org boolean override and emits a structured
`admin.org.feature.set` audit event. orgs.manage gates the endpoint
(superadmin short-circuits in the current permission scheme). Note
text never lands in the audit payload, only `note_present` does.

DELETE coverage lives in `test_admin_feature_overrides_delete.py`
(T15) and aggregate state coverage in `test_admin_feature_state.py`
(T16) — keep this file PUT-only.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.feature_override import OrgFeatureOverride
from app.models.user import Organization, Role, User
from app.routers.admin_orgs import router as admin_orgs_router
from app.security import hash_password


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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user(request: Request) -> User:
        request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
        return await current_user_resolver(session_factory)

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(admin_orgs_router)
    return app


async def _seed(factory) -> dict:
    """Two orgs: 'Admin Org' (with the superadmin) and 'Target' (the
    one we'll set overrides on)."""
    async with factory() as db:
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()
        sa = User(
            org_id=admin_org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=target.id, username="t_owner",
            email="t_owner@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {
            "admin_user_id": sa.id,
            "admin_email": sa.email,
            "admin_org_id": admin_org.id,
            "target_id": target.id,
            "plain_user_id": plain.id,
        }


def _superadmin_resolver():
    async def resolve(session_factory):
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


def _plain_user_resolver():
    async def resolve(session_factory):
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(False)))
            ).scalar_one()
    return resolve


# ── PUT happy-paths ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_sets_new_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    from app.routers import admin_orgs as admin_orgs_module
    with patch.object(admin_orgs_module.logger, "ainfo", new_callable=AsyncMock) as mock_ainfo:
        with TestClient(app) as client:
            res = client.put(
                f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
                json={"value": True, "note": "internal beta"},
            )
    assert res.status_code == 200
    body = res.json()
    assert body["feature_key"] == "ai.budget"
    assert body["value"] is True
    assert body["set_by"] == seed["admin_user_id"]
    assert body["set_by_email"] == seed["admin_email"]
    assert body["is_expired"] is False

    # Find the structured event among possibly-multiple ainfo calls.
    set_calls = [
        c for c in mock_ainfo.call_args_list
        if c.args and c.args[0] == "admin.org.feature.set"
    ]
    assert len(set_calls) == 1
    args, kwargs = set_calls[0]
    assert kwargs["target_org_id"] == seed["target_id"]
    assert kwargs["feature_key"] == "ai.budget"
    assert kwargs["old_value"] is None
    assert kwargs["new_value"] is True
    assert kwargs["note_present"] is True
    assert kwargs["actor_email"] == seed["admin_email"]
    # Note text NEVER lands in the audit payload.
    assert "note" not in kwargs or kwargs.get("note") is None
    assert "internal beta" not in str(kwargs.values())


@pytest.mark.asyncio
async def test_put_updates_existing_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        first = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.forecast",
            json={"value": True},
        )
        assert first.status_code == 200
        assert first.json()["value"] is True

        from app.routers import admin_orgs as admin_orgs_module
        with patch.object(admin_orgs_module.logger, "ainfo", new_callable=AsyncMock) as mock_ainfo:
            second = client.put(
                f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.forecast",
                json={"value": False, "note": "rolled back"},
            )

    assert second.status_code == 200
    body = second.json()
    assert body["value"] is False
    assert body["set_by_email"] == seed["admin_email"]

    set_calls = [
        c for c in mock_ainfo.call_args_list
        if c.args and c.args[0] == "admin.org.feature.set"
    ]
    assert len(set_calls) == 1
    args, kwargs = set_calls[0]
    assert kwargs["old_value"] is True
    assert kwargs["new_value"] is False
    assert kwargs["note_present"] is True


# ── PUT validation rejections ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_rejects_unknown_key(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.totally_made_up",
            json={"value": True},
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_put_strict_bool_rejects_string(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": "true"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_put_extra_fields_rejected(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True, "extra": "x"},
        )
    assert res.status_code == 422


# ── auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_requires_orgs_manage(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True},
        )
    assert res.status_code == 403


# ── DELETE ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_revokes_existing_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        put_res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True},
        )
        assert put_res.status_code == 200

        from app.routers import admin_orgs as admin_orgs_module
        with patch.object(admin_orgs_module.logger, "ainfo", new_callable=AsyncMock) as mock_ainfo:
            del_res = client.delete(
                f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            )

    assert del_res.status_code == 204
    assert del_res.content == b""

    revoked_calls = [
        c for c in mock_ainfo.call_args_list
        if c.args and c.args[0] == "admin.org.feature.revoked"
    ]
    assert len(revoked_calls) == 1
    args, kwargs = revoked_calls[0]
    assert kwargs["target_org_id"] == seed["target_id"]
    assert kwargs["feature_key"] == "ai.budget"
    assert kwargs["old_value"] is True
    assert kwargs["actor_user_id"] == seed["admin_user_id"]
    assert kwargs["actor_email"] == seed["admin_email"]


@pytest.mark.asyncio
async def test_delete_returns_404_when_no_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_key_returns_400(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.totally_made_up",
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_delete_requires_orgs_manage(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
        )
    assert res.status_code == 403


# ── Missing-org / set_at refresh (review fixes) ───────────────────────────


@pytest.mark.asyncio
async def test_put_returns_404_for_missing_org(session_factory):
    """A nonexistent target org_id must surface as 404, not as the
    FK-collision 409 "Override changed concurrently" message."""
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/admin/orgs/999999/feature-overrides/ai.budget",
            json={"value": True},
        )
    assert res.status_code == 404
    assert "Organization not found" in res.json().get("detail", "")


@pytest.mark.asyncio
async def test_delete_returns_404_for_missing_org(session_factory):
    """DELETE on a nonexistent org should also 404 with the explicit
    'Organization not found' message (not the override-not-found one)."""
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(
            "/api/v1/admin/orgs/999999/feature-overrides/ai.budget",
        )
    assert res.status_code == 404
    assert "Organization not found" in res.json().get("detail", "")


@pytest.mark.asyncio
async def test_put_refreshes_set_at_on_update(session_factory):
    """Updating an existing override must advance set_at so the UI
    can show 'last set at' rather than the original grant time."""
    from datetime import datetime
    from sqlalchemy import select as _select

    from app.models.feature_override import OrgFeatureOverride

    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    past = datetime(2026, 1, 1, 0, 0, 0)

    with TestClient(app) as client:
        first = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True},
        )
        assert first.status_code == 200

    # Force the seeded row's set_at into the past so the second PUT
    # has something concrete to advance past.
    async with session_factory() as db:
        row = (
            await db.execute(
                _select(OrgFeatureOverride).where(
                    OrgFeatureOverride.org_id == seed["target_id"],
                    OrgFeatureOverride.feature_key == "ai.budget",
                )
            )
        ).scalar_one()
        row.set_at = past
        await db.commit()

    with TestClient(app) as client:
        second = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": False},
        )
    assert second.status_code == 200
    new_set_at = second.json()["set_at"]
    # ISO-8601 is lexicographically ordered, so a string compare suffices,
    # but parsing is more explicit.
    assert datetime.fromisoformat(new_set_at) > past


# ── POST /feature-overrides/sweep-expired ─────────────────────────────────


async def _seed_overrides(factory, target_id: int, *, admin_user_id: int) -> None:
    """Plant 4 overrides on target org: 2 expired, 1 future, 1 NULL expiry."""
    from datetime import timedelta

    from app._time import utcnow_naive

    now = utcnow_naive()
    past_1 = now - timedelta(days=1)
    past_2 = now - timedelta(hours=1)
    future = now + timedelta(days=7)
    async with factory() as db:
        db.add_all([
            OrgFeatureOverride(
                org_id=target_id, feature_key="ai.budget",
                value=True, set_by=admin_user_id, expires_at=past_1,
            ),
            OrgFeatureOverride(
                org_id=target_id, feature_key="ai.forecast",
                value=True, set_by=admin_user_id, expires_at=past_2,
            ),
            OrgFeatureOverride(
                org_id=target_id, feature_key="ai.smart_plan",
                value=True, set_by=admin_user_id, expires_at=future,
            ),
            OrgFeatureOverride(
                org_id=target_id, feature_key="ai.autocategorize",
                value=True, set_by=admin_user_id, expires_at=None,
            ),
        ])
        await db.commit()


@pytest.mark.asyncio
async def test_sweep_deletes_only_expired_rows(session_factory):
    """Only rows whose expires_at is past NOW are deleted. Future-
    and NULL-expiry rows are untouched."""
    from sqlalchemy import select as _select

    seed = await _seed(session_factory)
    await _seed_overrides(
        session_factory, seed["target_id"], admin_user_id=seed["admin_user_id"]
    )
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")

    assert res.status_code == 200
    assert res.json() == {"deleted_count": 2}

    async with session_factory() as db:
        remaining_keys = sorted(
            (
                await db.execute(
                    _select(OrgFeatureOverride.feature_key).where(
                        OrgFeatureOverride.org_id == seed["target_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert remaining_keys == ["ai.autocategorize", "ai.smart_plan"]


@pytest.mark.asyncio
async def test_sweep_writes_audit_event(session_factory):
    """A successful sweep records exactly one
    ``admin.feature_override.expired_swept`` audit row carrying
    ``deleted_count`` in detail and no PII beyond the actor email."""
    from sqlalchemy import select as _select

    seed = await _seed(session_factory)
    await _seed_overrides(
        session_factory, seed["target_id"], admin_user_id=seed["admin_user_id"]
    )
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert res.status_code == 200

    async with session_factory() as db:
        rows = (
            await db.execute(
                _select(AuditEvent).where(
                    AuditEvent.event_type
                    == "admin.feature_override.expired_swept"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.actor_user_id == seed["admin_user_id"]
    assert row.actor_email == seed["admin_email"]
    assert row.target_org_id is None
    assert row.target_org_name is None
    # PR-C: bounded detail with per-row entries + counts.
    assert row.detail["deleted_count"] == 2
    assert row.detail["truncated_count"] == 0
    assert sorted(e["feature_key"] for e in row.detail["entries"]) == (
        ["ai.budget", "ai.forecast"]
    )
    # All entries reference the seeded target org.
    assert {e["org_id"] for e in row.detail["entries"]} == {seed["target_id"]}
    assert row.detail["counts_by_feature"] == {
        "ai.budget": 1,
        "ai.forecast": 1,
    }
    # Happy path: no divergence flag (or explicitly false). Pin the
    # contract so a future reader can tell happy-path detail from the
    # divergence-path detail at a glance.
    assert not row.detail.get("divergence")


@pytest.mark.asyncio
async def test_sweep_idempotent_when_nothing_expired(session_factory):
    """No expired rows → returns deleted_count: 0, audit row still written."""
    from sqlalchemy import select as _select

    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert res.status_code == 200
    assert res.json() == {"deleted_count": 0}

    async with session_factory() as db:
        rows = (
            await db.execute(
                _select(AuditEvent).where(
                    AuditEvent.event_type
                    == "admin.feature_override.expired_swept"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    # No expirees: zero counts everywhere, empty entries list. Happy
    # path (nothing was locked, nothing was deleted), so no divergence.
    assert rows[0].detail["deleted_count"] == 0
    assert rows[0].detail["truncated_count"] == 0
    assert rows[0].detail["entries"] == []
    assert rows[0].detail["counts_by_feature"] == {}
    assert not rows[0].detail.get("divergence")


@pytest.mark.asyncio
async def test_sweep_truncates_audit_entries_over_cap(session_factory):
    """When > _SWEEP_AUDIT_ENTRY_CAP rows are deleted, the audit
    detail caps the per-row `entries` list and surfaces the rest as
    `truncated_count` plus `counts_by_feature`.

    Pre-PR-C the audit row only carried `deleted_count`, so an ops
    person investigating a sweep could only see the total — not which
    orgs/features lost access.
    """
    from datetime import timedelta

    from sqlalchemy import select as _select

    from app._time import utcnow_naive
    from app.models.user import Organization
    from app.routers.admin_orgs import _SWEEP_AUDIT_ENTRY_CAP

    seed = await _seed(session_factory)
    cap = _SWEEP_AUDIT_ENTRY_CAP
    over_cap = cap + 5
    past = utcnow_naive() - timedelta(hours=1)

    # Need many distinct orgs because of the UNIQUE(org_id, feature_key).
    # Plant a single expired override per fresh org, alternating between
    # two keys so counts_by_feature is non-trivial.
    async with session_factory() as db:
        orgs = [
            Organization(name=f"OrgX-{i}", billing_cycle_day=1)
            for i in range(over_cap)
        ]
        db.add_all(orgs)
        await db.commit()
        for i, org in enumerate(orgs):
            db.add(
                OrgFeatureOverride(
                    org_id=org.id,
                    feature_key=("ai.budget" if i % 2 == 0 else "ai.forecast"),
                    value=True,
                    set_by=seed["admin_user_id"],
                    expires_at=past,
                )
            )
        await db.commit()

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert res.status_code == 200
    assert res.json()["deleted_count"] == over_cap

    async with session_factory() as db:
        row = (
            await db.execute(
                _select(AuditEvent).where(
                    AuditEvent.event_type
                    == "admin.feature_override.expired_swept"
                )
            )
        ).scalars().one()
    assert row.detail["deleted_count"] == over_cap
    assert len(row.detail["entries"]) == cap
    assert row.detail["truncated_count"] == over_cap - cap
    # Aggregate counts cover ALL deleted rows, not just the entries list.
    total_via_counts = sum(row.detail["counts_by_feature"].values())
    assert total_via_counts == over_cap
    # Happy path: no divergence under non-racing sweep.
    assert not row.detail.get("divergence")


@pytest.mark.asyncio
async def test_sweep_requires_orgs_manage(session_factory):
    """A non-superadmin without orgs.manage gets 403 and nothing is deleted."""
    from sqlalchemy import select as _select

    seed = await _seed(session_factory)
    await _seed_overrides(
        session_factory, seed["target_id"], admin_user_id=seed["admin_user_id"]
    )
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert res.status_code == 403

    async with session_factory() as db:
        count = (
            await db.execute(
                _select(OrgFeatureOverride).where(
                    OrgFeatureOverride.org_id == seed["target_id"]
                )
            )
        ).scalars().all()
    # All 4 rows still present.
    assert len(count) == 4


@pytest.mark.asyncio
async def test_sweep_audit_count_matches_actual_deletes(session_factory):
    """Regression: audit `deleted_count`, `entries`, and `counts_by_feature`
    must reflect rows that were ACTUALLY removed by this sweep, not the
    set of rows the sweep merely observed as expired.

    Pre-fix the route ran SELECT-then-DELETE-by-predicate. Two
    overlapping sweeps could both snapshot the same expired rows; the
    first deleted them, the second deleted 0 but still audited
    ``deleted_count = len(snapshot)``. With lock-then-delete-by-id +
    deleted_count derived from the DELETE rowcount, the second sweep
    audits exactly what it removed.

    We simulate the race deterministically by deleting some of the
    expired rows out-of-band BETWEEN the route's snapshot and its
    DELETE. SQLite ignores SELECT FOR UPDATE, so this hook reliably
    fires; on MySQL the FOR UPDATE serializes the second sweep until
    the first commits, achieving the same invariant.
    """
    from datetime import timedelta

    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select

    from app._time import utcnow_naive

    seed = await _seed(session_factory)
    await _seed_overrides(
        session_factory, seed["target_id"], admin_user_id=seed["admin_user_id"]
    )

    # Hook to fire between the route's snapshot SELECT and its
    # DELETE: delete ai.budget out-of-band so the sweep observes 2
    # expired rows but only actually deletes 1.
    saw_select = {"done": False}
    real_execute = AsyncSession.execute

    async def hooked_execute(self, statement, *args, **kwargs):
        sql = str(statement).lower()
        # Fire AFTER the snapshot SELECT, BEFORE the DELETE.
        if (
            not saw_select["done"]
            and "select" in sql
            and "org_feature_overrides" in sql
            and "expires_at" in sql
        ):
            saw_select["done"] = True
            result = await real_execute(self, statement, *args, **kwargs)
            # Out-of-band delete via a separate session so it commits
            # independently of the route's session.
            async with session_factory() as side_db:
                await side_db.execute(
                    _delete(OrgFeatureOverride).where(
                        OrgFeatureOverride.org_id == seed["target_id"],
                        OrgFeatureOverride.feature_key == "ai.budget",
                    )
                )
                await side_db.commit()
            return result
        return await real_execute(self, statement, *args, **kwargs)

    app = make_app(session_factory, _superadmin_resolver())
    with patch.object(AsyncSession, "execute", hooked_execute):
        with TestClient(app) as client:
            res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert res.status_code == 200
    # Only ai.forecast survived to be deleted by the route. ai.budget
    # was deleted out-of-band; the route's DELETE-by-id should not
    # count it.
    assert res.json() == {"deleted_count": 1}

    async with session_factory() as db:
        rows = (
            await db.execute(
                _select(AuditEvent).where(
                    AuditEvent.event_type
                    == "admin.feature_override.expired_swept"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    detail = rows[0].detail
    # Divergence path: the route locked 2 rows under SELECT FOR
    # UPDATE (no-op on SQLite) but only deleted 1 because the other
    # was removed out-of-band. We CANNOT honestly tell which of the
    # two locked rows our DELETE removed without DELETE ... RETURNING
    # (MySQL doesn't have it). The audit row records the count we
    # know is true and explicitly flags the gap; per-row identities
    # are omitted because asserting them would be a guess.
    assert detail["deleted_count"] == 1
    assert detail["locked_count"] == 2
    assert detail["divergence"] is True
    assert detail["divergence_reason"] == "concurrent_modification"
    assert "entries" not in detail
    assert "counts_by_feature" not in detail
    assert "truncated_count" not in detail


@pytest.mark.asyncio
async def test_sweep_zero_deletes_when_all_expired_rows_already_gone(session_factory):
    """Edge: every expired row is removed out-of-band between the
    route's snapshot and its DELETE. Sweep must audit deleted_count=0
    with empty entries — the symmetric of the partial-overlap case.
    """
    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select

    seed = await _seed(session_factory)
    await _seed_overrides(
        session_factory, seed["target_id"], admin_user_id=seed["admin_user_id"]
    )

    saw_select = {"done": False}
    real_execute = AsyncSession.execute

    async def hooked_execute(self, statement, *args, **kwargs):
        sql = str(statement).lower()
        if (
            not saw_select["done"]
            and "select" in sql
            and "org_feature_overrides" in sql
            and "expires_at" in sql
        ):
            saw_select["done"] = True
            result = await real_execute(self, statement, *args, **kwargs)
            async with session_factory() as side_db:
                await side_db.execute(
                    _delete(OrgFeatureOverride).where(
                        OrgFeatureOverride.org_id == seed["target_id"],
                        OrgFeatureOverride.feature_key.in_(
                            ["ai.budget", "ai.forecast"]
                        ),
                    )
                )
                await side_db.commit()
            return result
        return await real_execute(self, statement, *args, **kwargs)

    app = make_app(session_factory, _superadmin_resolver())
    with patch.object(AsyncSession, "execute", hooked_execute):
        with TestClient(app) as client:
            res = client.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert res.status_code == 200
    assert res.json() == {"deleted_count": 0}

    async with session_factory() as db:
        row = (
            await db.execute(
                _select(AuditEvent).where(
                    AuditEvent.event_type
                    == "admin.feature_override.expired_swept"
                )
            )
        ).scalars().one()
    # Divergence: route locked 2 rows, then both were deleted
    # out-of-band, so DELETE-by-id removed 0. Same shape as the
    # partial-overlap case. No per-row claims.
    assert row.detail["deleted_count"] == 0
    assert row.detail["locked_count"] == 2
    assert row.detail["divergence"] is True
    assert row.detail["divergence_reason"] == "concurrent_modification"
    assert "entries" not in row.detail
    assert "counts_by_feature" not in row.detail
    assert "truncated_count" not in row.detail
