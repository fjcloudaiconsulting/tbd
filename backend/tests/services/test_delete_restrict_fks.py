"""TBD-342 — user/org deletion must not trip a RESTRICT foreign key.

Four columns are declared ``ondelete="RESTRICT"``. Three had no service-layer
delete, so ``delete(User)`` / ``delete_org_cascade`` raised. Because
``dashboard.py::_get_or_create`` auto-creates a layout on first dashboard
access, EVERY user who has opened the dashboard has a ``dashboard_layouts``
row — so this was not an edge case, it broke deletion for essentially every
real org.

⚠ **Assert on ``IntegrityError``, never on the MySQL error number.** Measured:
SQLite DOES enforce RESTRICT here (the DDL emits ``ON DELETE RESTRICT`` and the
harness sets ``PRAGMA foreign_keys=ON``), but it reports
"FOREIGN KEY constraint failed" while MySQL reports 1451. A fence matching the
string "1451" would be RED on CI against correct code.
"""
import pytest
import pytest_asyncio
from datetime import datetime, timezone

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Organization, User
from app.models.dashboard import DashboardLayout
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.report import Report, ReportVisibility
from app.models.user import Role
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


async def _org_with_user(db, *, name="Acme", superadmin=False):
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    u = User(
        org_id=org.id, username=f"u{name}", email=f"u@{name}.io",
        password_hash=hash_password("pw-123456789"), role=Role.OWNER,
        is_superadmin=superadmin, is_active=True, email_verified=True,
    )
    db.add(u)
    await db.flush()
    return org, u


def _layout(user, org):
    """Exactly what dashboard.py::_get_or_create writes on first access."""
    from app.routers.dashboard import DEFAULT_CANVAS_FILTERS, DEFAULT_DASHBOARD_LAYOUT
    import copy

    return DashboardLayout(
        owner_user_id=user.id, org_id=org.id,
        layout_json=copy.deepcopy(DEFAULT_DASHBOARD_LAYOUT),
        canvas_filters_json=copy.deepcopy(DEFAULT_CANVAS_FILTERS),
    )


def _report(user, org):
    """reports.owner_user_id is RESTRICT against users.id — and the org path
    deletes users, so this bites delete_org_cascade too, not just delete_user."""
    return Report(
        owner_user_id=user.id, org_id=org.id, name="R",
        visibility=ReportVisibility.PRIVATE, layout_json={},
        canvas_filters_json={},
    )


def _credential(org):
    return OrgAICredential(org_id=org.id, provider=AiProvider.OPENAI)


def _ledger_row(org):
    return AIUsageLedger(
        org_id=org.id, feature_key="forecast.refine", model="gpt-x",
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
        est_cost_cents=1, dispatched_at=datetime.now(timezone.utc),
        latency_ms=1, success=True, retries_used=0,
    )


# ── the RESTRICT set is closed ─────────────────────────────────────────────


def test_every_restrict_fk_is_accounted_for():
    """DoD item 2 — the point is not to fix four columns, it is to make the
    FIFTH impossible to miss.

    ⚠ This fence is asserted PER DELETE PATH, not as a flat set. An earlier
    version listed `reports.owner_user_id` as "handled" because
    admin_users_service.delete_user handles it — and passed while
    delete_org_cascade still raised on that exact column. A column can be
    covered on the user path and uncovered on the org path; a membership test
    cannot see the difference, and that is precisely how the original defect
    survived its own fence.

    Kills: a new RESTRICT column with no service-layer delete, AND an existing
    one covered on only one of the two paths.
    """
    import pathlib

    from sqlalchemy import ForeignKeyConstraint

    # ⚠ Derived from Base.metadata, NOT a source regex. A regex over
    # `ondelete="RESTRICT"` is style-dependent and was measured to miss:
    # single quotes, a non-literal constant, `ForeignKey(User.id, ...)`, and
    # `ForeignKeyConstraint([...], ondelete="RESTRICT")` in __table_args__ —
    # which is ALREADY an in-repo idiom (models/org_ai_routing.py). It also
    # collapsed two RESTRICT FKs in the same file to one entry. The metadata
    # is what the DDL is actually built from, so it cannot drift from reality.
    found: set[tuple[str, str, str]] = set()
    for table in Base.metadata.tables.values():
        for c in table.constraints:
            if not isinstance(c, ForeignKeyConstraint):
                continue
            if (c.ondelete or "").upper() != "RESTRICT":
                continue
            for el in c.elements:
                found.add((table.name, el.parent.name, el.target_fullname))

    # (model file, referenced column) -> the delete paths that MUST clear it.
    #   "user" = admin_users_service.delete_user
    #   "org"  = admin_orgs_service.delete_org_cascade
    expected: dict[tuple[str, str, str], set[str]] = {
        ("reports", "owner_user_id", "users.id"): {"user", "org"},
        ("dashboard_layouts", "owner_user_id", "users.id"): {"user", "org"},
        ("org_ai_credentials", "org_id", "organizations.id"): {"org"},
        ("ai_usage_ledger", "org_id", "organizations.id"): {"org"},
    }
    assert found == set(expected), (
        "RESTRICT foreign keys changed.\n"
        f"  added:   {sorted(found - set(expected))}\n"
        f"  removed: {sorted(set(expected) - found)}\n"
        "A NEW RESTRICT FK needs a service-layer delete before its parent in "
        "EVERY path that deletes that parent — the user path and the org path "
        "are separate, and covering only one is how this bug shipped."
    )

    backend = pathlib.Path(__file__).resolve().parents[2]
    # Now prove each path actually clears what it owes. Source-level, because
    # a behavioural test can only cover fixtures someone remembered to seed.
    user_src = (backend / "app" / "services" / "admin_users_service.py").read_text()
    org_src = (backend / "app" / "services" / "admin_orgs_service.py").read_text()
    MODEL_FOR = {
        "reports": "Report",
        "dashboard_layouts": "DashboardLayout",
        "org_ai_credentials": "OrgAICredential",
        "ai_usage_ledger": "AIUsageLedger",
    }
    for (table_name, _holder, _target), paths in expected.items():
        model = MODEL_FOR[table_name]
        if "user" in paths:
            assert f"delete({model})" in user_src or f"update({model})" in user_src, (
                f"{model} is RESTRICT against users.id but "
                f"admin_users_service.delete_user never clears it"
            )
        if "org" in paths:
            assert f"delete({model})" in org_src, (
                f"{model} is RESTRICT but delete_org_cascade never clears it — "
                "the org path deletes users, so a users.id RESTRICT bites here "
                "too even when the user path already handles it"
            )


# ── user deletion ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_with_a_dashboard_layout_succeeds(session_factory):
    """The severe one. Every user who has opened the dashboard has a layout row.

    Kills: the shipped defect — delete(User) raising IntegrityError.
    """
    from app.services import admin_users_service

    async with session_factory() as db:
        org, u = await _org_with_user(db)
        # A distinct actor — delete_user refuses self-deletion.
        actor = User(
            org_id=org.id, username="actor", email="actor@acme.io",
            password_hash=hash_password("pw-123456789"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        db.add(actor)
        await db.flush()
        db.add(_layout(u, org))
        u.is_active = False  # delete_user requires an already-deactivated user
        await db.commit()
        uid, aid = u.id, actor.id

    async with session_factory() as db:
        await admin_users_service.delete_user(db, target_user_id=uid, actor_user_id=aid)
        await db.commit()

    async with session_factory() as db:
        assert (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none() is None
        rows = (
            await db.execute(select(DashboardLayout).where(DashboardLayout.owner_user_id == uid))
        ).scalars().all()
        assert rows == [], "the layout must be deleted, not orphaned"


# ── org deletion ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_org_with_ai_credential_and_usage_ledger_succeeds(session_factory):
    """Kills: delete_org_cascade raising on org_ai_credentials / ai_usage_ledger,
    which blocks deletion for any org that ever saved a credential or accrued
    usage."""
    from app.services import admin_orgs_service

    async with session_factory() as db:
        org, u = await _org_with_user(db)
        db.add_all([_layout(u, org), _report(u, org), _credential(org), _ledger_row(org)])
        await db.commit()
        oid = org.id

    async with session_factory() as db:
        await admin_orgs_service.delete_org_cascade(db, org_id=oid)
        await db.commit()

    async with session_factory() as db:
        assert (await db.execute(select(Organization).where(Organization.id == oid))).scalar_one_or_none() is None
        for model, col in (
            (Report, Report.org_id),
            (OrgAICredential, OrgAICredential.org_id),
            (AIUsageLedger, AIUsageLedger.org_id),
            (DashboardLayout, DashboardLayout.org_id),
        ):
            left = (await db.execute(select(model).where(col == oid))).scalars().all()
            assert left == [], f"{model.__name__} rows orphaned after org delete"


# ── the guard that repairing deletion ARMS (folded in from TBD-373) ────────


@pytest.mark.asyncio
async def test_delete_org_refuses_when_it_would_hard_delete_a_superadmin(session_factory):
    """⚠ This guard is only reachable BECAUSE deletion now works.

    Before this ticket, delete_org_cascade raised on a RESTRICT FK before it
    could destroy anything, so the unguarded superadmin hard-delete was masked
    in practice. Repairing deletion makes it live — so the guard ships in the
    same change, or the fix converts a fail-safe error into a working,
    unguarded, destructive path.

    Kills: shipping the RESTRICT repair alone.

    Note the harm is NOT privilege escalation — that reading was refuted: the
    actor's own row is structurally outside the delete set, so
    count(is_superadmin) >= 1 always. The harm is destroying platform admins
    with no operator signal, and anonymising their entire audit history via
    audit_events.actor_user_id ON DELETE SET NULL.
    """
    from app.services import admin_orgs_service
    from app.services.exceptions import ConflictError

    async with session_factory() as db:
        org, owner = await _org_with_user(db, name="Victim")
        sa = User(
            org_id=org.id, username="platsa", email="platsa@victim.io",
            password_hash=hash_password("pw-123456789"), role=Role.MEMBER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        db.add(sa)
        await db.flush()
        db.add_all([_layout(owner, org), _layout(sa, org)])
        await db.commit()
        oid, sa_id = org.id, sa.id

    async with session_factory() as db:
        with pytest.raises(ConflictError):
            await admin_orgs_service.delete_org_cascade(db, org_id=oid)

    async with session_factory() as db:
        assert (
            await db.execute(select(User).where(User.id == sa_id))
        ).scalar_one_or_none() is not None, "the superadmin must survive the refusal"


@pytest.mark.asyncio
async def test_delete_org_without_a_superadmin_still_succeeds(session_factory):
    """Control. Kills a blunt guard that refuses every org delete."""
    from app.services import admin_orgs_service

    async with session_factory() as db:
        org, u = await _org_with_user(db, name="Ordinary")
        db.add(_layout(u, org))
        await db.commit()
        oid = org.id

    async with session_factory() as db:
        await admin_orgs_service.delete_org_cascade(db, org_id=oid)
        await db.commit()

    async with session_factory() as db:
        assert (await db.execute(select(Organization).where(Organization.id == oid))).scalar_one_or_none() is None


# ── B2: blast radius — every new DELETE must keep its WHERE clause ─────────


@pytest.mark.asyncio
async def test_deleting_one_user_leaves_other_users_dashboards_alone(session_factory):
    """Kills an unscoped `delete(DashboardLayout)` — or one scoped to org_id
    instead of owner_user_id — either of which wipes every colleague's saved
    dashboard platform-wide while every other test stays green.

    The happy-path test gives the target a layout and the bystander none, so
    it cannot see this. This one gives the bystander a layout too.
    """
    from app.services import admin_users_service

    async with session_factory() as db:
        org, target = await _org_with_user(db)
        bystander = User(
            org_id=org.id, username="colleague", email="colleague@acme.io",
            password_hash=hash_password("pw-123456789"), role=Role.MEMBER,
            is_active=True, email_verified=True,
        )
        actor = User(
            org_id=org.id, username="actor", email="actor@acme.io",
            password_hash=hash_password("pw-123456789"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        db.add_all([bystander, actor])
        await db.flush()
        db.add_all([_layout(target, org), _layout(bystander, org)])
        target.is_active = False
        await db.commit()
        tid, bid, aid = target.id, bystander.id, actor.id

    async with session_factory() as db:
        await admin_users_service.delete_user(db, target_user_id=tid, actor_user_id=aid)
        await db.commit()

    async with session_factory() as db:
        survived = (
            await db.execute(
                select(DashboardLayout).where(DashboardLayout.owner_user_id == bid)
            )
        ).scalars().all()
        assert len(survived) == 1, "the bystander's dashboard must survive"


@pytest.mark.asyncio
async def test_deleting_one_org_leaves_another_orgs_rows_alone(session_factory):
    """Kills an unscoped `delete(...)` on any of the four org-path deletes —
    each of which would destroy every other tenant's data.

    The happy-path org test builds exactly one org, so it is blind to a
    missing `WHERE`.
    """
    from app.services import admin_orgs_service

    async with session_factory() as db:
        doomed, du = await _org_with_user(db, name="Doomed")
        keeper, ku = await _org_with_user(db, name="Keeper")
        db.add_all([
            _layout(du, doomed), _report(du, doomed),
            _credential(doomed), _ledger_row(doomed),
            _layout(ku, keeper), _report(ku, keeper),
            _credential(keeper), _ledger_row(keeper),
        ])
        await db.commit()
        doomed_id, keeper_id = doomed.id, keeper.id

    async with session_factory() as db:
        await admin_orgs_service.delete_org_cascade(db, org_id=doomed_id)
        await db.commit()

    async with session_factory() as db:
        for model, col in (
            (DashboardLayout, DashboardLayout.org_id),
            (Report, Report.org_id),
            (OrgAICredential, OrgAICredential.org_id),
            (AIUsageLedger, AIUsageLedger.org_id),
        ):
            kept = (await db.execute(select(model).where(col == keeper_id))).scalars().all()
            assert len(kept) == 1, f"{model.__name__} of the OTHER org was destroyed"
        assert (
            await db.execute(select(Organization).where(Organization.id == keeper_id))
        ).scalar_one_or_none() is not None


# ── N2: the is_active semantics of the guard, pinned ──────────────────────


@pytest.mark.asyncio
async def test_inactive_superadmin_also_blocks_org_delete(session_factory):
    """A DEACTIVATED superadmin still blocks deletion.

    Deliberate: the row still holds the platform flag, and hard-deleting it is
    the same irreversible harm — the account is gone and
    audit_events.actor_user_id nulls out, anonymising their history.
    Soft-deleted is recoverable (TBD-377); deleted is not.

    Kills: adding `User.is_active.is_(True)` to the guard's count, which would
    be GREEN against every other test in this file.
    """
    from app.services import admin_orgs_service
    from app.services.exceptions import ConflictError

    async with session_factory() as db:
        org, owner = await _org_with_user(db, name="HasDormant")
        sa = User(
            org_id=org.id, username="dormant", email="dormant@x.io",
            password_hash=hash_password("pw-123456789"), role=Role.MEMBER,
            is_superadmin=True, is_active=False, email_verified=True,
        )
        db.add(sa)
        await db.flush()
        db.add(_layout(owner, org))
        await db.commit()
        oid, sa_id = org.id, sa.id

    async with session_factory() as db:
        with pytest.raises(ConflictError) as excinfo:
            await admin_orgs_service.delete_org_cascade(db, org_id=oid)
        # Pin the machine-readable code: an implementation raising
        # ConflictError(msg) with code=None passes a bare `pytest.raises` and
        # then puts {"code": null} on the wire.
        assert excinfo.value.code == admin_orgs_service.CODE_ORG_HOLDS_SUPERADMIN

    async with session_factory() as db:
        assert (
            await db.execute(select(User).where(User.id == sa_id))
        ).scalar_one_or_none() is not None
