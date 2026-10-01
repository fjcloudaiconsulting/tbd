"""FENCE F-E2 (TBD-585): ``feature_service.get_entitlements`` is the ONE resolver.

Runtime half: patch ``feature_service.get_entitlements`` and every consumer
(``has_feature``, ``usage_service.admit``, ``get_ai_feature_status``, the
feature-state route) must follow. Kills a second resolver in admission, status
or feature-state, and a ``from ... import get_entitlements`` that binds the
function before the patch.

Static half: a function-granular AST scan over ``app/`` for every function
that touches the override tables or reads a plan's ``features`` /
``usage_limits``. The set of hits must EQUAL :data:`ALLOWED`; each entry names
why it may read them. A new reader either goes through the resolver or is
added here with a reason, in review.

⚠ Ceiling: the scan is lexical. ``getattr(plan, "fea" + "tures")``, a
``select(text(...))`` built outside ``text()``/``table()``, or a dynamic import
are invisible to it. The runtime half is what catches behaviour.
"""
from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import ai_status_service, feature_service, usage_service
from app.services.feature_service import UsageLimit
from app.routers.admin_orgs import router as admin_orgs_router
from tests.factories import make_test_app

# ── runtime half ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def factory(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


async def _seed(f) -> tuple[int, int]:
    async with f() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        db.add(User(org_id=org.id, username="root", email="r@x.io",
                    password_hash=hash_password("pw-1234567"), role=Role.OWNER,
                    is_superadmin=True, is_active=True, email_verified=True))
        await db.commit()
        return org.id, 0


def _patch(monkeypatch, *, features=None, limits=None):
    """Replace the resolver with one that returns a doctored Entitlements."""
    real = feature_service.get_entitlements

    async def fake(db, org_id, *, now=None):
        ent = await real(db, org_id, now=now)
        return dataclasses.replace(
            ent,
            features={**ent.features, **(features or {})},
            limits={**ent.limits, **(limits or {})},
            plan_features={**ent.plan_features, **(features or {})},
            plan_limits={**ent.plan_limits, **(limits or {})},
        )

    monkeypatch.setattr(feature_service, "get_entitlements", fake)


@pytest.mark.asyncio
async def test_has_feature_follows_the_resolver(factory, monkeypatch):
    org_id, _ = await _seed(factory)
    async with factory() as db:
        assert await feature_service.has_feature(db, org_id, "plans") is False
        _patch(monkeypatch, features={"plans": True})
        assert await feature_service.has_feature(db, org_id, "plans") is True


@pytest.mark.asyncio
async def test_admit_follows_the_resolver(factory, monkeypatch):
    org_id, _ = await _seed(factory)
    async with factory() as db:
        await usage_service.admit(db, org_id, "mcp.calls")  # default: unlimited
    _patch(monkeypatch, limits={"mcp.calls": UsageLimit("month", 0)})
    async with factory() as db:
        with pytest.raises(usage_service.PlanLimitReached):
            await usage_service.admit(db, org_id, "mcp.calls")


@pytest.mark.asyncio
async def test_ai_status_follows_the_resolver(factory, monkeypatch):
    org_id, _ = await _seed(factory)
    async with factory() as db:
        before = await ai_status_service.get_ai_feature_status(db, org_id=org_id)
        assert before["budget"]["entitled"] is False
        _patch(monkeypatch, features={"ai.budget": True})
        after = await ai_status_service.get_ai_feature_status(db, org_id=org_id)
    assert after["budget"]["entitled"] is True


@pytest.mark.asyncio
async def test_feature_state_route_follows_the_resolver(factory, monkeypatch):
    org_id, _ = await _seed(factory)
    _patch(
        monkeypatch,
        features={"plans": True},
        limits={"mcp.calls": UsageLimit("day", 3)},
    )

    async def resolve(sf):
        async with sf() as db:
            from sqlalchemy import select
            return (await db.execute(select(User))).scalar_one()

    app = make_test_app(factory, routers=admin_orgs_router, current_user=resolve,
                        override_session_factory=True)
    with TestClient(app) as c:
        body = c.get(f"/api/v1/admin/orgs/{org_id}/feature-state").json()
    assert {r["key"]: r["effective"] for r in body["features"]}["plans"] is True
    assert {r["meter"]: r["effective"] for r in body["limits"]}["mcp.calls"] == {
        "period": "day", "limit": 3}


# ── static half ─────────────────────────────────────────────────────────────

APP = Path(__file__).resolve().parents[2] / "app"
MODELS = {"OrgFeatureOverride", "OrgLimitOverride"}
ATTRS = {"features", "usage_limits"}
SQL = re.compile(r"plans\.features|usage_limits|org_feature_overrides|org_limit_overrides")
SQL_CALLS = {"text", "table"}


def scan_source(source: str) -> dict[str, set[str]]:
    """``{function qualname: {what it touches}}`` for one module's source.

    Reads the AST, so a comment or docstring naming a table matches nothing.
    Module-level code is ``<module>``. Plain imports are skipped (a module
    importing a model is not a reader), but an ALIASED import of a model is
    flagged so a rename cannot hide a use.
    """
    hits: dict[str, set[str]] = {}

    def add(scope: list[str], what: str) -> None:
        hits.setdefault(".".join(scope) or "<module>", set()).add(what)

    def walk(node: ast.AST, scope: list[str]) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(ch, scope + [ch.name])
                continue
            if isinstance(ch, (ast.Import, ast.ImportFrom)):
                for a in ch.names:
                    if a.name.split(".")[-1] in MODELS and a.asname:
                        add(scope, f"alias:{a.asname}")
                continue
            if isinstance(ch, ast.Name) and ch.id in MODELS:
                add(scope, ch.id)
            elif isinstance(ch, ast.Attribute) and (ch.attr in MODELS or ch.attr in ATTRS):
                add(scope, "." + ch.attr)
            elif isinstance(ch, ast.Call):
                f = ch.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name in SQL_CALLS:
                    for sub in ast.walk(ch):
                        if (
                            isinstance(sub, ast.Constant)
                            and isinstance(sub.value, str)
                            and (m := SQL.search(sub.value))
                        ):
                            add(scope, "sql:" + m.group(0))
            walk(ch, scope)

    walk(ast.parse(source), [])
    return hits


def scan_app() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for p in sorted(APP.rglob("*.py")):
        for fn in scan_source(p.read_text()):
            found.add((p.relative_to(APP).as_posix(), fn))
    return found


# (file, function) -> why it may touch the override tables / plan JSON.
ALLOWED: dict[tuple[str, str], str] = {
    ("services/feature_service.py", "get_entitlements"):
        "THE resolver: reads Plan.features/usage_limits and both override tables.",
    ("services/feature_service.py", "get_features"):
        "reads Entitlements.features (resolver output), not a plan column.",
    ("mcp_main.py", "mcp_endpoint"):
        "the MCP front door reads Entitlements.features/limits (resolver output) once "
        "per request for the F-E4 door; reads no plan column or override row.",
    ("routers/admin_orgs.py", "get_feature_state"):
        "display fields of the override rows + Entitlements.features; what is in "
        "force comes from ent.overridden.",
    ("routers/admin_orgs.py", "_override_to_response"): "serialises an override row it was handed.",
    ("routers/admin_orgs.py", "_limit_override_to_response"): "serialises an override row it was handed.",
    ("routers/admin_orgs.py", "set_feature_override"): "operator write of the feature override row.",
    ("routers/admin_orgs.py", "revoke_feature_override"): "operator delete of the feature override row.",
    ("routers/admin_orgs.py", "set_limit_override"): "operator write of the limit override row.",
    ("routers/admin_orgs.py", "revoke_limit_override"): "operator delete of the limit override row.",
    ("routers/admin_orgs.py", "sweep_expired_feature_overrides"):
        "deletes expired rows of both override tables; never decides entitlement.",
    ("routers/plans.py", "create_plan"): "plan WRITE path: canonicalises and audits the plan JSON.",
    ("routers/plans.py", "update_plan"): "plan WRITE path: canonicalises and audits the plan JSON.",
    ("routers/plans.py", "duplicate_plan"): "plan WRITE path: canonicalises and audits the plan JSON.",
    ("schemas/subscription.py", "PlanUpdate._usage_limits_not_null"):
        "validator on the request body's own field; reads no stored plan.",
    ("services/admin_orgs_service.py", "delete_org_cascade"): "erasure: deletes both override tables' rows.",
    ("services/user_merge_service.py", "merge_users"): "reassigns set_by attribution on both override tables.",
    ("services/admin_subscription_service.py", "_plan_to_dict"):
        "display-only plan serialisation for the admin subscription view (its own "
        "is_expired stays; follow-up only if it matters).",
    ("services/admin_subscription_service.py", "get_subscription_detail"):
        "display-only override listing for the admin subscription view; decides nothing.",
}


def test_fe2_only_the_allowlisted_functions_read_entitlement_storage():
    found = scan_app()
    allowed = set(ALLOWED)
    assert found == allowed, (
        "F-E2: the set of functions touching plan features/usage_limits or the "
        "override tables changed. "
        f"new (route it through feature_service.get_entitlements or allowlist with a "
        f"reason): {sorted(found - allowed)}; "
        f"gone (remove the stale entry): {sorted(allowed - found)}"
    )


def test_fe2_every_allowlist_entry_has_a_reason():
    assert all(len(reason) > 15 for reason in ALLOWED.values())


def test_fe2_scanner_sees_what_it_claims_and_ignores_comments():
    """The scanner is itself fenced: a scanner that silently finds nothing would
    make the set-equality above vacuous."""
    src = '''
"""docstring naming org_feature_overrides and OrgLimitOverride"""
from app.models.limit_override import OrgLimitOverride as LO  # org_limit_overrides
import sqlalchemy as sa

def by_name(db):
    return select(OrgFeatureOverride)

def by_attr(plan):
    return plan.usage_limits, plan.features

def by_sql():
    return sa.text("SELECT features FROM plans WHERE plans.features IS NULL")

def by_table():
    return table("org_limit_overrides")

def clean(db):
    "org_feature_overrides in a plain string, not SQL"
    return db.other
'''
    hits = scan_source(src)
    assert hits["by_name"] == {"OrgFeatureOverride"}
    assert hits["by_attr"] == {".usage_limits", ".features"}
    assert hits["by_sql"] == {"sql:plans.features"}
    assert hits["by_table"] == {"sql:org_limit_overrides"}
    assert hits["<module>"] == {"alias:LO"}
    assert "clean" not in hits
