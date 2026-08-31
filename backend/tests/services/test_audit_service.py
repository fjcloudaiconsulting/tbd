"""Service-layer tests for L4.7 audit_service.

Two properties to pin tightly:

1. ``record_audit_event`` opens its OWN session through the factory
   and commits — so an audit row exists even when the caller's session
   was rolled back.
2. ``record_audit_event`` NEVER raises. A broken factory must be
   absorbed; the caller's structlog event is the fallback channel.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.services.audit_service import (
    CSP_VIOLATION_EVENT_TYPE,
    list_audit_events,
    record_audit_event,
)


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


# ── recording ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_audit_event_commits_independently(session_factory):
    """Audit row is visible to a freshly-opened session — i.e. it was
    actually committed, not just flushed inside the recorder's
    not-yet-committed scope.
    """
    await record_audit_event(
        session_factory,
        event_type="admin.org.delete",
        actor_user_id=None,
        actor_email="root@example.io",
        target_org_id=None,
        target_org_name="Some Org",
        request_id="abc123",
        ip_address="10.0.0.1",
        outcome="success",
        detail={"k": "v"},
    )

    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "admin.org.delete"
    assert row.actor_email == "root@example.io"
    assert row.target_org_name == "Some Org"
    assert row.request_id == "abc123"
    assert row.ip_address == "10.0.0.1"
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.detail == {"k": "v"}


@pytest.mark.asyncio
async def test_record_audit_event_survives_bad_session():
    """If the factory itself blows up, record_audit_event must NOT
    raise — the structlog event the caller already emitted is the
    backup channel and we don't want to mask the original 200/500 the
    user sees.
    """
    def broken_factory():
        raise RuntimeError("DB unreachable")

    # Should not raise.
    await record_audit_event(
        broken_factory,
        event_type="admin.org.delete",
        actor_user_id=None,
        actor_email="root@example.io",
        target_org_id=None,
        target_org_name=None,
        request_id=None,
        ip_address=None,
        outcome="failure",
        detail=None,
    )


@pytest.mark.asyncio
async def test_record_audit_event_failure_logs_at_error():
    """A backstop audit-write failure surfaces at ERROR level.

    After PR-C the org-delete success path stages the audit row in
    the business txn (see ``add_audit_event_to_session``); the
    independent-session ``record_audit_event`` is now reserved for
    the failure path and the sweep. Either is operationally
    significant when it fails — a regression that pulls a caller
    back onto this swallow needs to fire alerts, not whisper at
    WARN. This pins the level so a future "oops" downgrade is
    caught.
    """
    from unittest.mock import AsyncMock, patch

    def broken_factory():
        raise RuntimeError("DB unreachable")

    from app.services import audit_service as audit_service_module

    with patch.object(
        audit_service_module.logger, "aerror", new_callable=AsyncMock
    ) as mock_aerror, patch.object(
        audit_service_module.logger, "awarning", new_callable=AsyncMock
    ) as mock_awarning:
        await record_audit_event(
            broken_factory,
            event_type="admin.org.delete",
            actor_user_id=None,
            actor_email="root@example.io",
            target_org_id=None,
            target_org_name=None,
            request_id=None,
            ip_address=None,
            outcome="failure",
            detail=None,
        )

    assert mock_awarning.call_count == 0, (
        "audit.record.failed must log at ERROR, not WARN"
    )
    assert mock_aerror.call_count == 1
    args, kwargs = mock_aerror.call_args
    assert args[0] == "audit.record.failed"
    assert kwargs["event_type"] == "admin.org.delete"
    assert kwargs["error_type"] == "RuntimeError"


# ── querying ────────────────────────────────────────────────────────────


async def _seed_three(factory) -> None:
    """Three rows: 2 success, 1 failure, spread across two timestamps."""
    base = datetime.datetime(2026, 5, 1, 9, 0, 0)
    async with factory() as db:
        db.add_all([
            AuditEvent(
                event_type="admin.org.delete",
                actor_user_id=None,
                actor_email="a@x.io",
                target_org_id=None,
                target_org_name="A",
                request_id="r1",
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail=None,
                created_at=base,
            ),
            AuditEvent(
                event_type="admin.org.delete.failed",
                actor_user_id=None,
                actor_email="b@x.io",
                target_org_id=None,
                target_org_name="B",
                request_id="r2",
                ip_address=None,
                outcome=AuditOutcome.FAILURE,
                detail=None,
                created_at=base + datetime.timedelta(hours=1),
            ),
            AuditEvent(
                event_type="admin.org.subscription.override",
                actor_user_id=None,
                actor_email="c@x.io",
                target_org_id=None,
                target_org_name="C",
                request_id="r3",
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail=None,
                created_at=base + datetime.timedelta(hours=2),
            ),
        ])
        await db.commit()


@pytest.mark.asyncio
async def test_list_audit_events_filters_by_outcome(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, outcome="failure")
    assert total == 1
    assert len(rows) == 1
    assert rows[0].event_type == "admin.org.delete.failed"


@pytest.mark.asyncio
async def test_list_audit_events_invalid_outcome_raises(session_factory):
    """Service-direct callers (and old swallow) used to silently skip
    the filter on a typo. PR-C tightens this: the route layer types
    the param as Literal so FastAPI rejects, and the service raises
    ValueError so any direct caller (tests, ad-hoc scripts) sees the
    typo loudly."""
    await _seed_three(session_factory)
    async with session_factory() as db:
        with pytest.raises(ValueError):
            await list_audit_events(db, outcome="failuer")


@pytest.mark.asyncio
async def test_list_audit_events_date_range(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(
            db,
            from_dt=datetime.datetime(2026, 5, 1, 9, 30, 0),
            to_dt=datetime.datetime(2026, 5, 1, 10, 30, 0),
        )
    # The middle event (10:00) is the only one inside the window.
    assert total == 1
    assert rows[0].event_type == "admin.org.delete.failed"


@pytest.mark.asyncio
async def test_list_audit_events_orders_newest_first(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db)
    assert total == 3
    # Newest first — the 11:00 row leads.
    assert rows[0].event_type == "admin.org.subscription.override"
    assert rows[-1].event_type == "admin.org.delete"


@pytest.mark.asyncio
async def test_list_audit_events_pagination(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, limit=2, offset=0)
    assert total == 3
    assert len(rows) == 2
    async with session_factory() as db:
        rows2, total2 = await list_audit_events(db, limit=2, offset=2)
    assert total2 == 3
    assert len(rows2) == 1


# ── TBD-439: the anonymous CSP stream must not bury the default audit view ──
#
# ``POST /api/v1/security/csp-report`` is public, takes no credential, and
# writes one audit row per report body -- 20 per request at 60/minute, so 1200
# rows/min from a single anonymous IP. Ten such requests fill page 1 of
# /admin/audit, every row ``outcome=failure`` / ``actor_email=anonymous``.
#
# ``routers/security.py`` has always DOCUMENTED that the mitigation is
# consumer-side. Nothing implemented it: before this change the string
# ``security.csp_violation`` appeared only in that router.
#
# ⚠ These are BEHAVIOURAL, deliberately. The ticket's DoD warns against
# asserting by grepping for the event-type string, because it already appears
# in ``security.py``'s own docstring -- "a grep is satisfied by a comment" has
# bitten this repo three times. Seeding rows and reading the result back
# cannot be satisfied by a comment.


async def _seed_csp_and_normal(factory) -> None:
    """3 CSP rows + 3 ordinary rows, CSP newest so it would dominate page 1.

    ⚠ The third ordinary row is anonymous AND a failure, deliberately. Without
    it every CSP row was ``actor_email="anonymous"`` + ``FAILURE`` and every
    ordinary row was the mirror image on both columns, so an implementation
    keying on the WRONG COLUMN -- ``actor_email != "anonymous"``, or
    ``outcome != FAILURE`` -- passed all four tests. That is the "fixture
    where the right and wrong implementations agree" class. An anonymous
    failed login is a realistic row and it discriminates the columns.
    """
    base = datetime.datetime(2026, 5, 1, 9, 0, 0)
    async with factory() as db:
        db.add_all(
            [
                AuditEvent(
                    event_type="admin.org.delete",
                    actor_user_id=None,
                    actor_email="a@x.io",
                    target_org_id=None,
                    target_org_name="A",
                    request_id="r1",
                    ip_address=None,
                    outcome=AuditOutcome.SUCCESS,
                    detail=None,
                    created_at=base,
                ),
                AuditEvent(
                    event_type="user.login.failed",
                    actor_user_id=None,
                    # Anonymous AND a failure, like a CSP row, but NOT a CSP
                    # row -- this is what kills the wrong-column mutants.
                    actor_email="anonymous",
                    target_org_id=None,
                    target_org_name=None,
                    request_id="r3",
                    ip_address="198.51.100.9",
                    outcome=AuditOutcome.FAILURE,
                    detail=None,
                    created_at=base + datetime.timedelta(minutes=30),
                ),
                AuditEvent(
                    event_type="user.login.success",
                    actor_user_id=None,
                    actor_email="b@x.io",
                    target_org_id=None,
                    target_org_name="B",
                    request_id="r2",
                    ip_address=None,
                    outcome=AuditOutcome.SUCCESS,
                    detail=None,
                    created_at=base + datetime.timedelta(hours=1),
                ),
            ]
            + [
                AuditEvent(
                    event_type=CSP_VIOLATION_EVENT_TYPE,
                    actor_user_id=None,
                    actor_email="anonymous",
                    target_org_id=None,
                    target_org_name=None,
                    request_id=f"csp{i}",
                    ip_address="203.0.113.7",
                    outcome=AuditOutcome.FAILURE,
                    detail=None,
                    # Newest, so an unfiltered newest-first query returns
                    # these THREE before either real row.
                    created_at=base + datetime.timedelta(hours=2 + i),
                )
                for i in range(3)
            ]
        )
        await db.commit()


@pytest.mark.asyncio
async def test_default_view_excludes_csp_violations(session_factory):
    """Mutant killed: dropping the ``else`` exclusion branch.

    Without it the three CSP rows -- being newest -- are the first three rows
    of the default view, which is the burying this ticket exists to stop.
    """
    await _seed_csp_and_normal(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db)

    assert [r.event_type for r in rows] == [
        "user.login.success",
        "user.login.failed",
        "admin.org.delete",
    ]
    assert all(r.event_type != CSP_VIOLATION_EVENT_TYPE for r in rows)


@pytest.mark.asyncio
async def test_default_view_total_also_excludes_csp_violations(session_factory):
    """⚠ The half that is easy to get wrong, and silent when you do.

    The exclusion has to reach the COUNT query as well as the row query. If it
    is applied to rows only, ``total`` still counts the hidden rows and the
    table advertises pages it cannot produce -- an off-by-1200/min pagination
    bug that looks like nothing on page 1.

    ⚠ Mutant killed: dropping ``count_q = count_q.where(clause)`` from the
    loop that fans ``where`` onto both queries. An earlier version of this
    docstring said "appending the clause to ``base`` instead of to ``where``"
    -- but ``base`` is not defined until below the where-building block, so
    that literal edit is a ``NameError``, not a silent mutant. Naming a mutant
    that cannot exist is the failure the fourth test in this file polices, so
    it is corrected here rather than left as a plausible-sounding sentence.
    """
    await _seed_csp_and_normal(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db)

    assert total == 3, "total must count only what the default view returns"
    assert len(rows) == total


@pytest.mark.asyncio
async def test_exclusion_is_applied_in_sql_not_after_the_limit(session_factory):
    """The row-side twin of the total fence, and it was missing.

    Mutant killed: filtering CSP out in PYTHON after the query, while the
    count query excludes correctly. That passes every other test in this file
    -- `total` is right and the returned rows are right whenever the page is
    big enough to hold them.

    With ``limit=2`` it breaks: SQL fetches the 2 NEWEST rows, which are both
    CSP, and the Python filter then drops them, returning 0 rows while
    ``total`` says 3. That is precisely "advertises pages it cannot produce",
    in the direction the total fence does not cover.
    """
    await _seed_csp_and_normal(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, limit=2)

    assert total == 3
    assert len(rows) == 2, "a full page must be returned, not a page of leftovers"
    assert all(r.event_type != CSP_VIOLATION_EVENT_TYPE for r in rows)


@pytest.mark.asyncio
async def test_include_csp_returns_the_complete_log_in_time_order(session_factory):
    """The escape hatch: a COMPLETE audit log must remain obtainable.

    Filtering by name returns CSP rows ALONE, so it cannot show a CSP probe
    interleaved with the auth attempts it correlates with. Without this flag
    ``/admin/audit`` -- the only read path over this table -- could not
    produce a complete log at all, which is not an acceptable property for a
    security log.

    Mutant killed: ignoring ``include_csp`` (excluding regardless).
    """
    await _seed_csp_and_normal(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, include_csp=True)

    assert total == 6, "3 ordinary + 3 CSP"
    assert len(rows) == 6
    # Interleaved in time order, newest first — the property the by-name
    # filter cannot give you.
    assert rows[0].event_type == CSP_VIOLATION_EVENT_TYPE
    assert rows[-1].event_type == "admin.org.delete"
    assert any(r.event_type != CSP_VIOLATION_EVENT_TYPE for r in rows)


@pytest.mark.asyncio
async def test_csp_violations_are_returned_when_asked_for_by_name(session_factory):
    """Control, and the direction that stops this becoming a censor.

    Mutant killed: excluding UNCONDITIONALLY (appending the ``!=`` clause
    outside the ``else``). That passes both fences above while making the
    stream unreachable, so an operator investigating a CSP incident would be
    told there is nothing to see.
    """
    await _seed_csp_and_normal(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(
            db, event_type=CSP_VIOLATION_EVENT_TYPE
        )

    assert total == 3
    assert len(rows) == 3
    assert all(r.event_type == CSP_VIOLATION_EVENT_TYPE for r in rows)


@pytest.mark.asyncio
async def test_filtering_by_another_event_type_is_unaffected(session_factory):
    """FENCE. Two mutants kill this one and nothing else.

    ⚠ This was briefly labelled a mere regression guard, on the grounds that
    no mutant killed it uniquely. That was wrong in the safe direction, and
    review found the two that do -- both newly REACHABLE because this diff
    puts ``CSP_VIOLATION_EVENT_TYPE`` in scope one line above the filter:

      1. ``where.append(AuditEvent.event_type == CSP_VIOLATION_EVENT_TYPE)``
         instead of ``== event_type`` -- a copy-paste slip. Every explicit
         filter then silently returns CSP rows instead of what was asked for.
      2. ``if event_type == CSP_VIOLATION_EVENT_TYPE:`` instead of
         ``if event_type:`` -- every non-CSP filter falls into the else and
         gets the whole default view instead of its own rows.

    Each fails ONLY this test. Under-claiming a fence is cheaper than
    over-claiming one, but both are drift between a justification and its
    mechanism, so the mutants are named rather than disclaimed.
    """
    await _seed_csp_and_normal(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, event_type="user.login.success")

    assert total == 1
    assert [r.event_type for r in rows] == ["user.login.success"]
