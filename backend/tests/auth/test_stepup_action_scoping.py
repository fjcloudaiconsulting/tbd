"""TBD-390: the SSO step-up proof is scoped to ONE action.

``users.stepup_token`` stores ``f"{action}:{token}"``; the fragment carries
the bare token; each consumer only accepts a proof issued for its own action
and spends it with an atomic compare-and-clear. Fences F1-F11 + D1 from the
spec; every rejection has a positive control on the same fixture shape.

All consumer cells drive the REAL auth seam (JWT -> ``get_current_user`` on
the request's own session), with a fresh ``password_set=False`` superadmin,
MFA off, live expiry, limiter reset.
"""
from __future__ import annotations

import ast
import itertools
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app as app_pkg
from app.auth.stepup import STEPUP_ACTIONS, consume_stepup, issue_stepup, stepup_valid
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.feature_override import OrgFeatureOverride
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import agent_tokens as agent_tokens_module
from app.routers import api_tokens as api_tokens_module
from app.routers import auth as auth_module
from app.routers import users as users_module
from app.security import create_access_token, hash_password, verify_password
from app.services import notification_service

NEW_PASSWORD = "brand-new-password-1"
SUCCEEDED_EVENT = "auth.google.sso_stepup.callback.succeeded"


# ── harness ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _mock_email(monkeypatch):
    monkeypatch.setattr(
        notification_service, "send_notification_email", AsyncMock(return_value=None)
    )


@pytest.fixture
def google_config(monkeypatch):
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")


def _client(factory) -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.include_router(users_module.router)
    app.include_router(api_tokens_module.router)
    app.include_router(agent_tokens_module.router)
    app.include_router(auth_module.router)
    return TestClient(app)


async def _seed(factory) -> int:
    """Fresh password_set=False superadmin, MFA off, no proof on the row."""
    tag = uuid.uuid4().hex[:10]
    async with factory() as s:
        org = Organization(name=f"Org {tag}", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        # ai.agent so the agent_token_mint consumer's feature gate admits it.
        s.add(OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True))
        u = User(
            org_id=org.id,
            username=f"u{tag}",
            email=f"u{tag}@acme.io",
            password_hash=hash_password("unusable-sso-fill"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
            password_set=False,
            mfa_enabled=False,
        )
        s.add(u)
        await s.commit()
        return u.id


async def _issue(factory, uid: int, action: str) -> str:
    async with factory() as s:
        user = await s.get(User, uid)
        tok = issue_stepup(user, action)
        await s.commit()
    return tok


async def _set_raw(factory, uid: int, stored: str) -> None:
    """Write a stored value verbatim (legacy / crafted rows), live expiry."""
    async with factory() as s:
        await s.execute(
            update(User)
            .where(User.id == uid)
            .values(
                stepup_token=stored,
                stepup_token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
            )
        )
        await s.commit()


async def _row(factory, uid: int) -> tuple:
    """Scalar snapshot from a FRESH session."""
    async with factory() as s:
        u = await s.get(User, uid)
        return (u.stepup_token, u.stepup_token_expires_at)


async def _user(factory, uid: int) -> User:
    async with factory() as s:
        return await s.get(User, uid)


@dataclass(frozen=True)
class Consumer:
    method: str
    path: str
    body: Callable[[str, int], dict]
    ok: int
    reject_status: int
    reject_detail: str
    module: ModuleType


# One consumer per action. The grid below is generated from STEPUP_ACTIONS x
# this dict, and ``test_every_action_has_exactly_one_consumer`` fails the day
# an action is added without a consumer here.
CONSUMERS: dict[str, Consumer] = {
    "email_change": Consumer(
        "PUT",
        "/api/v1/users/me",
        lambda tok, uid: {"email": f"moved{uid}@acme.io", "stepup_token": tok},
        200,
        400,
        "Step-up verification with Google is required to change email",
        users_module,
    ),
    "password_set": Consumer(
        "POST",
        "/api/v1/users/me/password",
        lambda tok, uid: {"new_password": NEW_PASSWORD, "stepup_token": tok},
        204,
        400,
        "Step-up verification with Google is required to set a password",
        users_module,
    ),
    "pat_mint": Consumer(
        "POST",
        "/api/v1/system/api-tokens",
        lambda tok, uid: {
            "name": "cron",
            "scope": "write",
            "expires_in_days": 30,
            "stepup_token": tok,
        },
        201,
        401,
        "Step-up verification required",
        api_tokens_module,
    ),
    "agent_token_mint": Consumer(
        "POST",
        "/api/v1/agent/tokens",
        lambda tok, uid: {
            "name": "harness",
            "scope": "agent:write",
            "expires_in_days": 30,
            "stepup_token": tok,
        },
        201,
        401,
        "Step-up verification required",
        agent_tokens_module,
    ),
}


async def _call(factory, uid: int, action: str, tok: str):
    c = CONSUMERS[action]
    u = await _user(factory, uid)
    jwt = create_access_token(u.id, u.org_id, u.role.value)
    with _client(factory) as client:
        return client.request(
            c.method,
            c.path,
            json=c.body(tok, uid),
            headers={"Authorization": f"Bearer {jwt}"},
        )


def _assert_rejected(res, action: str) -> None:
    c = CONSUMERS[action]
    assert res.status_code == c.reject_status, res.text
    assert res.json()["detail"] == c.reject_detail, res.text


def _assert_ok(res, action: str) -> None:
    assert res.status_code == CONSUMERS[action].ok, res.text


def test_every_action_has_exactly_one_consumer():
    assert set(CONSUMERS) == set(STEPUP_ACTIONS)


# ── F1: the action x consumer grid ──────────────────────────────────────────


@pytest.mark.parametrize(
    "issued,consumer", list(itertools.product(STEPUP_ACTIONS, CONSUMERS))
)
async def test_f1_grid_only_the_issued_action_accepts_the_proof(factory, issued, consumer):
    """Kills: action-agnostic compare; one consumer left unconverted; a
    wrong-action attempt that burns AND commits the token. (An uncommitted
    burn is rolled back by the handler's raise, so a
    consume-before-validate mutant is caught here only where a failure path
    commits: pat_mint's audit on the shared test connection.)"""
    uid = await _seed(factory)
    tok = await _issue(factory, uid, issued)
    before = await _row(factory, uid)

    res = await _call(factory, uid, consumer, tok)

    if issued == consumer:
        _assert_ok(res, consumer)
        assert await _row(factory, uid) == (None, None)
        return
    _assert_rejected(res, consumer)
    assert await _row(factory, uid) == before
    # Positive control on the SAME row: the proof survived and still works
    # at the consumer it was issued for.
    _assert_ok(await _call(factory, uid, issued, tok), issued)


# ── F2-F5: malformed / crafted presentations ────────────────────────────────


@pytest.mark.parametrize("action", list(CONSUMERS))
async def test_f2_legacy_bare_token_is_rejected_everywhere(factory, action):
    """Kills: a "no prefix = any action" fallback."""
    tok = "legacy-bare-token-" + "x" * 20
    uid = await _seed(factory)
    await _set_raw(factory, uid, tok)
    _assert_rejected(await _call(factory, uid, action, tok), action)

    control = await _seed(factory)
    await _set_raw(factory, control, f"{action}:{tok}")
    _assert_ok(await _call(factory, control, action, tok), action)


@pytest.mark.parametrize("action", list(CONSUMERS))
async def test_f3_presenting_the_stored_prefixed_value_is_rejected(factory, action):
    """Kills: comparing presented against stored without server-side
    prefixing (``presented == stored`` / ``stored.endswith(presented)``)."""
    uid = await _seed(factory)
    tok = await _issue(factory, uid, action)
    _assert_rejected(await _call(factory, uid, action, f"{action}:{tok}"), action)
    _assert_ok(await _call(factory, uid, action, tok), action)


@pytest.mark.parametrize("action", list(CONSUMERS))
async def test_f4_right_action_wrong_token_is_rejected(factory, action):
    """Kills: a prefix-only / startswith compare."""
    uid = await _seed(factory)
    tok = await _issue(factory, uid, action)
    wrong = tok[:-1] + ("A" if tok[-1] != "A" else "B")
    assert len(wrong) == len(tok)
    _assert_rejected(await _call(factory, uid, action, wrong), action)
    _assert_ok(await _call(factory, uid, action, tok), action)


@pytest.mark.parametrize("action", list(CONSUMERS))
async def test_f5_non_ascii_token_is_a_rejection_not_a_500(factory, action):
    """Kills: a str-level ``compare_digest`` (TypeError on non-ASCII -> 500)."""
    uid = await _seed(factory)
    tok = await _issue(factory, uid, action)
    _assert_rejected(await _call(factory, uid, action, "é" + tok[1:]), action)
    _assert_ok(await _call(factory, uid, action, tok), action)


# ── F6: initiate requires a known action ────────────────────────────────────


def _sets_oauth_state(res) -> bool:
    return any(c.startswith("oauth_state=") for c in res.headers.get_list("set-cookie"))


async def _initiate(factory, uid: int, **kwargs):
    u = await _user(factory, uid)
    jwt = create_access_token(u.id, u.org_id, u.role.value)
    with _client(factory) as client:
        return client.post(
            "/api/v1/auth/sso-stepup/initiate",
            headers={"Authorization": f"Bearer {jwt}"},
            **kwargs,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"json": {}},
        {"json": {"return_to": "security"}},
        {"json": {"action": "admin"}},
        {"json": {"action": "PAT_MINT"}},
    ],
    ids=["no_body", "empty", "legacy_return_to", "unknown", "case_folded"],
)
async def test_f6_initiate_rejects_missing_or_unknown_action(factory, google_config, kwargs):
    """Kills: a silent default action / case folding."""
    uid = await _seed(factory)
    res = await _initiate(factory, uid, **kwargs)
    assert res.status_code == 422, res.text
    assert not _sets_oauth_state(res), res.headers.get_list("set-cookie")


@pytest.mark.parametrize("action", list(STEPUP_ACTIONS))
async def test_f6_initiate_encodes_the_action_in_state(factory, google_config, action):
    uid = await _seed(factory)
    res = await _initiate(factory, uid, json={"action": action})
    assert res.status_code == 200, res.text
    assert _sets_oauth_state(res)
    state = res.cookies.get("oauth_state")
    assert state.startswith(f"stepup:{uid}:")
    assert state.endswith(f":{action}")
    assert len(state.split(":")) == 4


# ── F7/F8: callback ─────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _patch_google(monkeypatch, email: str) -> None:
    class _Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_a, **_k):
            return _Resp({"access_token": "fake-google-access-token"})

        async def get(self, *_a, **_k):
            return _Resp({"email": email, "verified_email": True})

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda *a, **k: _Fake())


async def _all_audit_rows(factory) -> list[AuditEvent]:
    async with factory() as s:
        return list((await s.execute(select(AuditEvent))).scalars().all())


def _dump(row: AuditEvent) -> str:
    return json.dumps(
        {c.name: getattr(row, c.name) for c in row.__table__.columns}, default=str
    )


def _callback(factory, state: str):
    with _client(factory) as client:
        client.cookies.set("oauth_state", state)
        return client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )


@pytest.mark.parametrize("action", list(STEPUP_ACTIONS))
async def test_f7_callback_issues_a_scoped_proof_and_audits_success(
    factory, google_config, monkeypatch, action
):
    """Kills: prefix leaking into the fragment; wrong redirect; a missing or
    token-leaking success audit."""
    uid = await _seed(factory)
    u = await _user(factory, uid)
    _patch_google(monkeypatch, u.email)

    init = await _initiate(factory, uid, json={"action": action})
    assert init.status_code == 200, init.text
    res = _callback(factory, init.cookies.get("oauth_state"))

    assert res.status_code == 302, res.text
    location = res.headers["location"]
    base, frag = location.split("#stepup_token=", 1)
    assert base == f"http://localhost{STEPUP_ACTIONS[action]}"
    assert frag and ":" not in frag
    assert (await _row(factory, uid))[0] == f"{action}:{frag}"

    rows = await _all_audit_rows(factory)
    succeeded = [r for r in rows if r.event_type == SUCCEEDED_EVENT]
    assert len(succeeded) == 1, [r.event_type for r in rows]
    assert succeeded[0].detail == {"action": action}
    assert succeeded[0].outcome == "success"
    assert succeeded[0].actor_user_id == uid
    for r in rows:
        assert frag not in _dump(r)


@pytest.mark.parametrize("slot", ["settings", "security", "admin"])
async def test_f8_callback_rejects_a_state_slot_that_is_not_an_action(
    factory, google_config, monkeypatch, slot
):
    """Kills: trusting slot 3 (the pre-TBD-390 return keys included)."""
    uid = await _seed(factory)
    u = await _user(factory, uid)
    _patch_google(monkeypatch, u.email)

    res = _callback(factory, f"stepup:{uid}:nonce:{slot}")
    assert res.status_code == 307, res.text
    assert res.headers["location"] == "http://localhost/settings?sso_stepup_error=state"
    assert await _row(factory, uid) == (None, None)
    assert [r for r in await _all_audit_rows(factory) if r.event_type == SUCCEEDED_EVENT] == []

    # Control: the same cookie/state round trip with a real action gets past
    # the state checks and issues a proof.
    ok = _callback(factory, f"stepup:{uid}:nonce:email_change")
    assert ok.status_code == 302, ok.text
    assert ok.headers["location"].startswith("http://localhost/settings#stepup_token=")


# ── F9: consume is atomic, and a lost race is a rejection ───────────────────


async def _side_effect_free(factory, uid: int, action: str) -> None:
    u = await _user(factory, uid)
    if action == "email_change":
        assert u.pending_email is None
        assert u.email.startswith("u")
    elif action == "password_set":
        assert u.password_set is False
        assert not verify_password(NEW_PASSWORD, u.password_hash)
    else:
        async with factory() as s:
            n = await s.scalar(
                select(func.count()).select_from(ApiToken).where(
                    ApiToken.created_by_user_id == uid
                )
            )
        assert n == 0
        failures = [
            r
            for r in await _all_audit_rows(factory)
            if r.event_type
            == ("agent_token.created" if action == "agent_token_mint" else "api_token.created")
            and r.outcome == "failure"
            and r.actor_user_id == uid
        ]
        assert len(failures) == 1
        assert failures[0].detail["reason"] == "step_up_failed"


@pytest.mark.parametrize("action", list(CONSUMERS))
async def test_f9_consumer_rejects_when_consume_loses_the_race(factory, monkeypatch, action):
    """Kills: an ORM-assign-only consume, and a consumer ignoring the
    helper's ``False``. The fake rolls back like the real helper does, so a
    consumer that reads ``current_user`` after ``False`` surfaces as a 500."""
    control = await _seed(factory)
    _assert_ok(await _call(factory, control, action, await _issue(factory, control, action)), action)

    async def fake(db, user):
        await db.rollback()
        return False

    monkeypatch.setattr(CONSUMERS[action].module, "consume_stepup", fake)
    uid = await _seed(factory)
    tok = await _issue(factory, uid, action)
    _assert_rejected(await _call(factory, uid, action, tok), action)
    await _side_effect_free(factory, uid, action)


async def test_f9_d1_consume_with_a_stale_row_fails_and_releases(factory):
    """Helper-level: another session spent the proof after we validated it.
    SQLite proves the WHERE clause, not a MySQL row-lock race.

    D1: on False the helper has rolled back (no open txn holding the row)."""
    uid = await _seed(factory)
    tok = await _issue(factory, uid, "email_change")
    async with factory() as a:
        user = await a.get(User, uid)
        assert stepup_valid(user, tok, "email_change")
        await a.commit()  # expire_on_commit=False: the ORM row stays stale
        async with factory() as b:
            await b.execute(update(User).where(User.id == uid).values(stepup_token=None))
            await b.commit()

        assert await consume_stepup(a, user) is False
        assert not a.in_transaction()

    stored, expires = await _row(factory, uid)
    assert stored is None and expires is not None  # as the other session left it
    # No second-session "lock released" write here: under the test StaticPool
    # every session shares one connection, so it could not go red. The
    # ``in_transaction()`` assert above is the D1 fence.


async def test_consume_live_row_control(factory):
    uid = await _seed(factory)
    await _issue(factory, uid, "pat_mint")
    async with factory() as a:
        user = await a.get(User, uid)
        assert await consume_stepup(a, user) is True
        assert user.stepup_token is None and user.stepup_token_expires_at is None
        assert user not in a.dirty
        await a.commit()
    assert await _row(factory, uid) == (None, None)


# ── F10: consume clears both fields durably ─────────────────────────────────


@pytest.mark.parametrize("action", list(CONSUMERS))
async def test_f10_success_clears_both_fields(factory, action):
    uid = await _seed(factory)
    tok = await _issue(factory, uid, action)
    _assert_ok(await _call(factory, uid, action, tok), action)
    assert await _row(factory, uid) == (None, None)


# ── F11: one reader/writer ──────────────────────────────────────────────────

APP_ROOT = Path(app_pkg.__file__).resolve().parent
NAMES = {"stepup_token", "stepup_token_expires_at"}
OWNER = {"auth/stepup.py", "models/user.py"}
# main.py: ``stepup_token`` is a NAME in the 422 redaction set (TBD-578), a
# request-body key, never the column; attribute access there is still caught.
CONSTANT_OK = OWNER | {"services/export_registry.py", "main.py"}


def _violations(rel: str, tree: ast.AST) -> list[str]:
    out: list[str] = []
    constant_ok = rel in CONSTANT_OK or rel.startswith("schemas/")
    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute) and node.attr in NAMES and rel not in OWNER:
            receiver_is_body = isinstance(node.value, ast.Name) and node.value.id == "body"
            if node.attr == "stepup_token_expires_at" or not receiver_is_body:
                out.append(f"{where} .{node.attr}")
        elif isinstance(node, ast.keyword) and node.arg in NAMES and not constant_ok:
            out.append(f"{where} {node.arg}=")
        elif isinstance(node, ast.Constant) and node.value in NAMES and not constant_ok:
            out.append(f"{where} {node.value!r}")
        elif isinstance(node, ast.Call) and rel not in OWNER:
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "text":
                for arg in ast.walk(node):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "stepup_token" in arg.value:
                        out.append(f"{where} text(...stepup_token...)")
    return out


def _scan() -> list[str]:
    found: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        found += _violations(rel, ast.parse(path.read_text(), filename=str(path)))
    return found


def test_f11_only_the_stepup_module_touches_the_proof_columns():
    """Kills: a fourth consumer (TBD-578) re-inlining the compare."""
    assert _scan() == []


def test_f11_detector_catches_a_reinlined_compare():
    src = "def f(current_user, body):\n    return current_user.stepup_token == body.stepup_token\n"
    assert _violations("routers/new.py", ast.parse(src)) == ["routers/new.py:2 .stepup_token"]
    assert _violations("routers/new.py", ast.parse("getattr(u, 'stepup_token')"))
    assert _violations("routers/new.py", ast.parse("u.stepup_token_expires_at"))
    assert _violations("routers/new.py", ast.parse("text('select stepup_token from users')"))
    assert _violations("routers/new.py", ast.parse("update(User).values(stepup_token=None)"))
