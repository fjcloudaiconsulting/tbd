"""Router tests for L4.7 GET /api/v1/admin/audit.

Pins:
- The auth gate (audit.view → superadmin short-circuit; non-superadmin
  gets 403).
- Response shape and ordering (newest first).
- Pagination (limit/offset surface total + items correctly).
"""
from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.user import Organization, Role, User
from app.routers.admin_audit import router as admin_audit_router
from app.security import hash_password
from tests.factories import make_test_app


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
    return make_test_app(
        session_factory,
        routers=admin_audit_router,
        current_user=current_user_resolver,
    )


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Audit Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        sa = User(
            org_id=org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=org.id, username="user",
            email="u@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {"org_id": org.id, "sa_id": sa.id, "plain_id": plain.id}


async def _seed_events(factory, n: int) -> None:
    base = datetime.datetime(2026, 5, 1, 9, 0, 0)
    async with factory() as db:
        for i in range(n):
            db.add(
                AuditEvent(
                    event_type=f"admin.org.event.{i}",
                    actor_user_id=None,
                    actor_email=f"actor-{i}@x.io",
                    target_org_id=None,
                    target_org_name=f"Org-{i}",
                    request_id=f"req-{i}",
                    ip_address="10.0.0.1",
                    outcome=AuditOutcome.SUCCESS,
                    detail={"i": i},
                    created_at=base + datetime.timedelta(minutes=i),
                )
            )
        await db.commit()


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


@pytest.mark.asyncio
async def test_get_audit_list_requires_superadmin(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 1)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_get_audit_list_returns_events(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 3)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # Newest first — index 2 is the last seeded (latest timestamp).
    assert body["items"][0]["event_type"] == "admin.org.event.2"
    assert body["items"][0]["outcome"] == "success"
    assert body["items"][0]["request_id"] == "req-2"
    assert body["items"][0]["target_org_name"] == "Org-2"
    assert body["items"][0]["detail"] == {"i": 2}


@pytest.mark.asyncio
async def test_get_audit_list_pagination(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 5)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit?limit=2&offset=0")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        res2 = client.get("/api/v1/admin/audit?limit=2&offset=4")
        body2 = res2.json()
        assert body2["total"] == 5
        assert len(body2["items"]) == 1


# ── sort contract (shared list contract) ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_audit_list_sort_event_type_asc(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 5)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/audit",
            params={"sort_by": "event_type", "sort_dir": "asc"},
        )
    assert res.status_code == 200
    types = [item["event_type"] for item in res.json()["items"]]
    assert types == sorted(types)


@pytest.mark.asyncio
async def test_get_audit_list_sort_actor_email_desc(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 5)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/audit",
            params={"sort_by": "actor_email", "sort_dir": "desc"},
        )
    assert res.status_code == 200
    emails = [item["actor_email"] for item in res.json()["items"]]
    assert emails == sorted(emails, reverse=True)


@pytest.mark.asyncio
async def test_get_audit_list_default_sort_is_created_at_desc(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 4)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit")
    assert res.status_code == 200
    created = [item["created_at"] for item in res.json()["items"]]
    assert created == sorted(created, reverse=True)


@pytest.mark.asyncio
async def test_get_audit_list_invalid_sort_by_returns_400(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 1)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit", params={"sort_by": "not_a_column"})
    assert res.status_code == 400
    assert "invalid_sort_by" in res.json()["detail"]


@pytest.mark.asyncio
async def test_get_audit_list_invalid_sort_dir_returns_400(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 1)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit", params={"sort_dir": "sideways"})
    assert res.status_code == 400
    assert "invalid_sort_dir" in res.json()["detail"]


# ── outcome filter validation (PR-C / PR #139 #3) ──────────────────────────


@pytest.mark.asyncio
async def test_get_audit_list_invalid_outcome_returns_422(session_factory):
    """A typo'd outcome (e.g. `failuer`) must return 422, not silently
    skip the filter and return all events. Catches the bug where the
    service's ``except ValueError: pass`` was swallowing typos."""
    await _seed(session_factory)
    await _seed_events(session_factory, 3)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit?outcome=failuer")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_audit_list_outcome_success_filters(session_factory):
    """The valid value `success` filters as before."""
    await _seed(session_factory)
    base = datetime.datetime(2026, 5, 1, 9, 0, 0)
    async with session_factory() as db:
        db.add_all([
            AuditEvent(
                event_type="admin.x.success",
                actor_user_id=None, actor_email="a@x.io",
                target_org_id=None, target_org_name=None,
                request_id=None, ip_address=None,
                outcome=AuditOutcome.SUCCESS, detail=None,
                created_at=base,
            ),
            AuditEvent(
                event_type="admin.x.failure",
                actor_user_id=None, actor_email="a@x.io",
                target_org_id=None, target_org_name=None,
                request_id=None, ip_address=None,
                outcome=AuditOutcome.FAILURE, detail=None,
                created_at=base + datetime.timedelta(minutes=1),
            ),
        ])
        await db.commit()

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit?outcome=success")
        assert res.status_code == 200
        assert res.json()["total"] == 1
        assert res.json()["items"][0]["outcome"] == "success"

        res2 = client.get("/api/v1/admin/audit?outcome=failure")
        assert res2.status_code == 200
        assert res2.json()["total"] == 1
        assert res2.json()["items"][0]["outcome"] == "failure"


# ── TBD-188 · api_token_id read path ─────────────────────────────────────────
#
# The column is useless write-only. §5 of
# ``specs/2026-08-07-audit-api-token-attribution.md`` rules the API surface
# complete (filter + query param + response field) and the UI out of scope
# (TBD-349, which needs the operator's visual approval). These pin both
# halves of that ruling.


# The two acting tokens. ⚠ Deliberately NOT 1 and 2: a seed whose token ids
# happen to equal the audit rows' own primary keys cannot tell
# ``AuditEvent.api_token_id == :v`` from ``AuditEvent.id == :v``, and the
# whole fence below stays green against the latter. They are also not any
# seeded ``users.id`` (``_seed`` creates ids 1 and 2), so the filter cannot
# be satisfied by an actor-user column either.
TOKEN_A = 7
TOKEN_B = 9


async def _seed_token_attributed_events(factory, org_id: int) -> None:
    """Three rows: acting token A, acting token B, and an un-attributed NULL.

    ⚠ Three things here are load-bearing, all of them anti-vacuity:

    * **The NULL row and the second token.** With a single non-NULL row an
      implementation that ignores the filter entirely returns ``total == 1``
      too, and the fence proves nothing.
    * **The token ids are 7/9, not 1/2, and the NULL row is inserted first.**
      Otherwise ``api_token_id`` and the audit row's own PK coincide and the
      filter is indistinguishable from ``AuditEvent.id == :v``.
    * **The NULL row's ``detail`` NAMES token A.** ``list_audit_events``
      claims in a comment that a row merely mentioning the token in
      ``detail`` (the ``api_token.created`` / ``.revoked`` shape) is
      excluded. Without this row that claim is untested prose.
    """
    base = datetime.datetime(2026, 8, 7, 9, 0, 0)
    async with factory() as db:
        db.add_all([
            ApiToken(
                id=tid,
                token_hash=f"hash-{tid}" + "0" * 40,
                token_prefix=f"pat_{tid}",
                name=f"token-{tid}",
                scope="write",
                created_by_user_id=None,
                created_by_email="root@platform.io",
                expires_at=base + datetime.timedelta(days=30),
            )
            for tid in (TOKEN_A, TOKEN_B)
        ])
        await db.commit()
        db.add_all([
            # Inserted FIRST so the lowest audit PK belongs to the row that
            # must be EXCLUDED by every ``api_token_id`` filter.
            AuditEvent(
                event_type="api_token.created",
                actor_user_id=None, actor_email="root@platform.io",
                target_org_id=org_id, target_org_name=None,
                api_token_id=None,
                request_id=None, ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                # Names token A as the SUBJECT, not the acting credential.
                detail={"api_token_id": TOKEN_A},
                created_at=base + datetime.timedelta(minutes=2),
            ),
            AuditEvent(
                event_type="tag.created",
                actor_user_id=None, actor_email="root@platform.io",
                target_org_id=org_id, target_org_name=None,
                api_token_id=TOKEN_A,
                request_id=None, ip_address=None,
                outcome=AuditOutcome.SUCCESS, detail=None,
                created_at=base,
            ),
            AuditEvent(
                event_type="tag.created",
                actor_user_id=None, actor_email="root@platform.io",
                target_org_id=org_id, target_org_name=None,
                api_token_id=TOKEN_B,
                request_id=None, ip_address=None,
                outcome=AuditOutcome.SUCCESS, detail=None,
                created_at=base + datetime.timedelta(minutes=1),
            ),
        ])
        await db.commit()

    # Self-fence on the paragraph above: prove no seeded row's own PK equals
    # its ``api_token_id``. If a future edit reverts the ids to 1/2 this
    # fires here instead of leaving the filter fence silently vacuous.
    async with factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        collisions = [r.id for r in rows if r.api_token_id == r.id]
        assert not collisions, (
            "seeded audit rows whose PK equals their api_token_id "
            f"({collisions}) — the filter fence could not tell "
            "AuditEvent.api_token_id from AuditEvent.id"
        )


@pytest.mark.asyncio
async def test_get_audit_list_filters_by_api_token_id(session_factory):
    """``?api_token_id=7`` selects only that token's rows, and the response
    carries the field.

    Both assertions are required and pin different mutants:
      * deleting the ``where`` clause in ``list_audit_events`` reddens
        ``total`` (3 instead of 1) but not the field;
      * deleting ``api_token_id`` from ``AuditEventResponse`` reddens the
        field but not ``total``.

    Named mutant M6c — ``where.append(AuditEvent.id == api_token_id)``, the
    filter reading the audit row's own primary key. It is only reachable
    because ``_seed_token_attributed_events`` keeps token ids off the PK
    sequence; see the anti-vacuity notes there before changing the seed.
    """
    seeded = await _seed(session_factory)
    await _seed_token_attributed_events(session_factory, seeded["org_id"])

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/audit?api_token_id={TOKEN_A}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["total"] == 1, (
            "the api_token_id filter did not narrow the result set "
            f"(got {body['total']}, want 1 of 3 seeded rows)"
        )
        assert body["items"][0]["api_token_id"] == TOKEN_A, (
            "api_token_id is missing from the audit response schema — the "
            "column is write-only from the operator's side"
        )
        # The filter matches the ACTING credential only. The seeded
        # ``api_token.created`` row names TOKEN_A in ``detail`` with a NULL
        # column; ``list_audit_events`` claims in a comment that such a row
        # is excluded, and this is what makes that claim testable.
        assert body["items"][0]["event_type"] == "tag.created"
        assert body["items"][0]["detail"] is None, (
            "the filter returned a row that merely NAMES the token in "
            "detail — actor and subject have been merged"
        )

        # The second token proves the filter selects, rather than merely
        # excluding NULLs.
        other = client.get(f"/api/v1/admin/audit?api_token_id={TOKEN_B}").json()
        assert other["total"] == 1
        assert other["items"][0]["api_token_id"] == TOKEN_B

        # And unfiltered still sees all three, so the seed is what we think.
        assert client.get("/api/v1/admin/audit").json()["total"] == 3


@pytest.mark.asyncio
async def test_get_audit_list_api_token_id_zero_is_422(session_factory):
    """``ge=1`` mirrors ``actor_user_id``: 0 is a client bug, not
    "unfiltered"."""
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/audit?api_token_id=0").status_code == 422


@pytest.mark.asyncio
async def test_get_audit_list_sort_by_api_token_id_is_400(session_factory):
    """``api_token_id`` is deliberately NOT sortable (§5).

    You filter by a token id, you never order by one. ``_SORTABLE``'s keys
    are the frontend's sort tokens, so an entry with no ``SortableHeader``
    behind it is dead surface on a closed whitelist. This pins the exclusion
    so a later "helpful" addition has to be deliberate.

    ⚠ This test passes against pre-TBD-188 code — verified by AST against
    ``origin/main``, whose ``_SORTABLE`` is
    ``{created_at, event_type, outcome, actor_email, target_org_name}``.
    A green is therefore NOT evidence that the feature works; it is a
    forward-looking invariant guard on an absence.
    """
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit?sort_by=api_token_id")
        assert res.status_code == 400, res.text
        assert res.json()["detail"] == "invalid_sort_by"
