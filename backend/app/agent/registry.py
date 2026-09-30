"""Agent tool registry (TBD-559).

One registry that both agent front doors (the in-app assistant and the MCP
server) call in-process. A tool is a thin adapter over an existing service; the
registry owns everything that must not vary between tools:

* ``ToolContext`` is built HERE and nowhere else, with ``org_id`` read from the
  authenticated ``User`` row. Tools read ``ctx.org_id``; their args models
  forbid extra fields and carry no tenancy field, so a model cannot name an org.
* ``invoke`` applies the gates in order, all fail-closed: args validation, the
  ``mcp.calls`` meter (mcp channel), the ``ai.agent`` plan feature, the
  product-area switch, the role rank, and the principal's scope.
* ``register`` refuses a half-declared tool, a bad name, a ``write`` tool that
  mirrors a DELETE route, and any tool mirroring a never-exposable area.

Write and sensitive tools never run from ``invoke``: it hands them to
``app.agent.actions`` (preview, then confirm), which owns the pending-action
table, the limits and the execution. ``confirm_action`` / ``cancel_action``
here are the front doors' only way to decide a staged action, so this module
stays the one place a ``ToolContext`` is built.

The ``mcp.calls`` meter is admitted HERE (TBD-585), on the mcp channel only:
once per ``invoke`` after args validation and before the other gates, and once
per ``confirm_action`` / ``cancel_action``. The auto path counts once (it
confirms inside the engine, not through ``confirm_action``). ``assistant.turns``
is admitted by the in-app front door, per turn; token-keyed rate limits (gate 6)
by the MCP front door work (561/578).
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Role, User
from app.services import feature_service, usage_service
from app.services.exceptions import NotFoundError, ValidationError
from app.services.feature_gate import Feature, resolve_feature

logger = structlog.stdlib.get_logger()

AGENT_FEATURE_KEY = "ai.agent"

# Providers accept only ``[a-zA-Z0-9_-]`` in function names; dots would break
# the in-app ``tools=`` payload. Names are a public contract once shipped.
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# ``Role`` is an unordered ``str`` Enum: ``>=`` on it compares the VALUES
# lexically ("admin" < "member" < "owner"), so an ADMIN would fail a MEMBER
# gate. Rank explicitly.
ROLE_RANK: dict[Role, int] = {Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}

Risk = Literal["read", "write", "sensitive"]
Channel = Literal["in_app", "mcp"]

# Agent token scopes (issued by the agent-token work that builds on this).
# ``agent:read`` may invoke only ``read`` tools; the wider scopes may invoke all.
AGENT_SCOPES = frozenset({"agent:read", "agent:write", "agent:auto"})

# Never exposable, whatever the risk class: credentials and tokens, members,
# invitations, roles, email, password, MFA, ``feature.``/``orgpref.`` settings,
# AI routing and caps, consents, org rename and wipe, data export, sharing,
# subscription and billing. Matched against the mirrored route's path.
_NEVER_EXPOSE_PREFIXES: tuple[str, ...] = (
    "/api/v1/admin",            # platform surface
    "/api/v1/ai",               # AI features: provider egress and spend
    "/api/v1/agent",            # agent tokens and the confirm surface itself
    "/api/v1/auth",             # login, password, MFA, email
    "/api/v1/feedback",         # sends mail
    "/api/v1/oauth",            # MCP authorization
    "/api/v1/orgs",             # members, invitations, roles, rename, wipe
    "/api/v1/plans",            # subscription plans
    "/api/v1/public",           # unauthenticated surfaces
    "/api/v1/scheduler",        # org scheduler settings
    "/api/v1/security",         # CSP report sink
    "/api/v1/settings/ai-providers",  # AI credentials, routing, caps, consent
    "/api/v1/subscriptions",    # billing
    "/api/v1/system",           # REST API tokens
    "/api/v1/users",            # email, password, profile
    "/api/v1/webhooks",         # provider callbacks
)
# ``/api/v1/settings`` itself and ``/{key}`` hold ``feature.``/``orgpref.``
# keys; ``/features/{feature}`` writes the product-area switches; the manual
# balance adjustment switch is an org toggle of the same kind.
_NEVER_EXPOSE_EXACT: frozenset[str] = frozenset(
    {
        "/api/v1/settings", "/api/v1/settings/{key}", "/api/v1/settings/features/{feature}",
        "/api/v1/settings/manual-balance-adjustment",
    }
)
# Any route with one of these path segments, wherever it is mounted.
_NEVER_EXPOSE_SEGMENTS: frozenset[str] = frozenset(
    {
        "api-tokens", "consent", "consents", "credentials", "export", "exports",
        "invitations", "members", "mfa", "password", "roles", "share", "sharing",
        "tokens",
    }
)

# Result keys whose string values a user, an import file or a bank feed can
# write. Attacker-influenceable text reaches the model only inside
# ``{"untrusted": ...}``, so a harness can tell data from instructions (advisory
# only: the structural guarantees are no egress and confirm-gated writes).
UNTRUSTED_KEYS: frozenset[str] = frozenset(
    {
        "name", "name_normalized", "description", "notes", "memo", "payee",
        "account_name", "account_type_name", "category_name", "parent_name",
        "linked_account_name",
    }
)

_ROUTE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


class ToolError(Exception):
    """A refusal or failure the front door returns to the agent as a result.

    ``code`` is stable and machine-readable; ``detail`` is safe to show.
    """

    def __init__(self, code: str, detail: str = "", data: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        # Machine-readable extras for the front door (e.g. the fresh preview of
        # ``preview_stale``, the recorded status of ``action_already_decided``).
        self.data = data or {}


def _limit_error(exc: usage_service.PlanLimitReached) -> ToolError:
    return ToolError(
        "plan_limit_reached",
        f"{exc.meter} limit reached for this {exc.period}",
        data={
            "meter": exc.meter, "limit": exc.limit, "period": exc.period,
            "resets_at": exc.resets_at.isoformat(),
        },
    )


async def _admit_mcp_call(db: AsyncSession, user: User, channel: str) -> None:
    """Count one ``mcp.calls`` for an mcp-channel call, or refuse it.

    Commits ``db`` (see ``usage_service.admit``): nothing may be written in
    this session before it.
    """
    if channel != "mcp":
        return
    try:
        await usage_service.admit(db, user.org_id, "mcp.calls")
    except usage_service.PlanLimitReached as exc:
        raise _limit_error(exc) from None


class ToolRegistrationError(Exception):
    """Raised at import time for a tool that must not be registered."""


@dataclass(frozen=True)
class ToolContext:
    db: AsyncSession
    user: User
    org_id: int
    channel: Channel
    api_token_id: int | None


@dataclass(frozen=True)
class Change:
    """One field of one entity a write would change, as the server computed it.

    ``before`` / ``after`` are JSON scalars (a Decimal is rendered as a fixed
    2dp string by the tool, so equal values fingerprint equal).
    """

    entity: str
    id: Any
    field: str
    before: Any
    after: Any
    currency: str | None = None


@dataclass
class Preview:
    """What a write tool says it will do. ``summary`` never embeds a
    user-writable string (it is not wrapped): user text goes in ``context``
    under a key in :data:`UNTRUSTED_KEYS`.

    A ``preview`` hook must read fresh (``populate_existing``): the auto path
    re-previews in the same session, whose identity map may hold a stale row."""

    summary: str
    changes: list[Change]
    warnings: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk: Risk
    args: type[BaseModel]
    # ``None`` means "no product-area switch" and must be written explicitly.
    product_area: Feature | None
    min_role: Role
    mirrors_route: tuple[str, str]  # ("GET", "/api/v1/budgets")
    description: str
    run: Callable[[ToolContext, Any], Awaitable[Any]] | None = None
    preview: Callable[..., Awaitable[Any]] | None = None
    execute: Callable[..., Awaitable[Any]] | None = None


_TOOLS: dict[str, ToolSpec] = {}


def _never_exposable(path: str) -> bool:
    if path in _NEVER_EXPOSE_EXACT:
        return True
    if any(path == p or path.startswith(p + "/") for p in _NEVER_EXPOSE_PREFIXES):
        return True
    return any(seg in _NEVER_EXPOSE_SEGMENTS for seg in path.strip("/").split("/"))


def wrap_untrusted(value: Any) -> Any:
    """Return ``value`` with every string under an :data:`UNTRUSTED_KEYS` key
    replaced by ``{"untrusted": <string>}``, recursively."""
    if isinstance(value, dict):
        return {
            k: {"untrusted": v} if k in UNTRUSTED_KEYS and isinstance(v, str) else wrap_untrusted(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [wrap_untrusted(v) for v in value]
    return value


def register(spec: ToolSpec) -> ToolSpec:
    """Add ``spec`` to the registry, refusing anything half-declared or unsafe."""
    if not TOOL_NAME_RE.fullmatch(spec.name):
        raise ToolRegistrationError(f"bad tool name {spec.name!r}")
    if spec.name in _TOOLS:
        raise ToolRegistrationError(f"duplicate tool name {spec.name!r}")
    if spec.risk not in ("read", "write", "sensitive"):
        raise ToolRegistrationError(f"{spec.name}: unknown risk {spec.risk!r}")
    if spec.args.model_config.get("extra") != "forbid":
        raise ToolRegistrationError(f"{spec.name}: args model must forbid extra fields")
    if not isinstance(spec.min_role, Role):
        raise ToolRegistrationError(f"{spec.name}: min_role must be a Role")
    if spec.product_area is not None and not isinstance(spec.product_area, Feature):
        raise ToolRegistrationError(f"{spec.name}: product_area must be a Feature or None")
    method, path = spec.mirrors_route
    if method not in _ROUTE_METHODS or not path.startswith("/api/v1/"):
        raise ToolRegistrationError(f"{spec.name}: bad mirrors_route {spec.mirrors_route!r}")
    if _never_exposable(path):
        raise ToolRegistrationError(f"{spec.name}: {path} is never exposable to agents")
    if spec.risk == "read":
        if spec.run is None or spec.preview is not None or spec.execute is not None:
            raise ToolRegistrationError(f"{spec.name}: a read tool declares run only")
        if method != "GET":
            raise ToolRegistrationError(f"{spec.name}: a read tool must mirror a GET")
    else:
        if spec.run is not None or spec.preview is None or spec.execute is None:
            raise ToolRegistrationError(
                f"{spec.name}: a {spec.risk} tool declares preview and execute, not run"
            )
        if method == "GET":
            raise ToolRegistrationError(f"{spec.name}: a {spec.risk} tool cannot mirror a GET")
        if spec.risk == "write" and method == "DELETE":
            raise ToolRegistrationError(
                f"{spec.name}: a delete is never 'write'; declare it 'sensitive'"
            )
    _TOOLS[spec.name] = spec
    return spec


def get_tool(name: str) -> ToolSpec | None:
    return _TOOLS.get(name)


def all_tools() -> list[ToolSpec]:
    return list(_TOOLS.values())


async def call_mapped(fn: Callable[..., Awaitable[Any]], *a: Any) -> Any:
    """Await a tool hook, mapping the service layer's refusals to ``ToolError``."""
    try:
        return await fn(*a)
    except NotFoundError as exc:
        raise ToolError("not_found", str(exc)) from None
    except ValidationError as exc:
        raise ToolError("invalid_arguments", str(exc)) from None


async def invoke(
    db: AsyncSession,
    user: User,
    name: str,
    raw_args: dict[str, Any] | None,
    *,
    channel: Channel,
    scope: str | None = None,
    api_token_id: int | None = None,
) -> dict[str, Any]:
    """Run tool ``name`` for the authenticated ``user``; return ``{"data": ...}``.

    ``scope`` is the agent token's scope on the MCP channel and must be
    ``None`` in-app (a browser session has full tool access subject to the
    other gates). A write or sensitive tool is previewed, not run: the result
    is the staged action (or, for ``agent:auto`` on a ``write`` tool, its
    executed outcome). Raises :class:`ToolError` on any refusal.
    """
    # Read once: a rollback below expires the ORM user, and a lazy load in
    # the log call would raise MissingGreenlet.
    org_id, user_id = user.org_id, user.id
    try:
        data = await _gate_and_run(db, user, name, raw_args, channel, scope, api_token_id)
    except ToolError as exc:
        await logger.ainfo(
            "agent.tool.invoked", tool=name, channel=channel, org_id=org_id,
            user_id=user_id, outcome=exc.code,
        )
        exc.data = wrap_untrusted(exc.data)
        raise
    except Exception:
        # Gates and tools alike: never hand SQL or internals to a model or a
        # harness. Roll back so the caller's session stays usable for the
        # next call in the same turn.
        await logger.aexception("agent.tool.failed", tool=name, channel=channel, org_id=org_id)
        if db is not None:
            await db.rollback()
        raise ToolError("internal_error", "the tool failed") from None
    await logger.ainfo(
        "agent.tool.invoked", tool=name, channel=channel, org_id=org_id, user_id=user_id,
        outcome="ok",
    )
    return {"data": wrap_untrusted(data)}


async def check_gates(
    db: AsyncSession, user: User, spec: ToolSpec, channel: str, scope: str | None
) -> None:
    """Gates 2-5 for ``spec`` against the CURRENT state of the org and the
    principal. Shared by ``invoke`` and by confirm, which re-runs them because
    the world may have changed since the preview."""
    org_id = user.org_id
    # 2. Plan entitlement.
    if not await feature_service.has_feature(db, org_id, AGENT_FEATURE_KEY):
        raise ToolError("feature_not_entitled", AGENT_FEATURE_KEY)
    # 3. Product area (env floor, operator switch, tenant opt-out).
    if spec.product_area is not None and not await resolve_feature(spec.product_area, org_id, db):
        raise ToolError("feature_disabled", spec.product_area.value)
    # 4. Role.
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK[spec.min_role]:
        raise ToolError("insufficient_role", spec.min_role.value)
    # 5. Principal scope.
    if channel == "in_app":
        if scope is not None:
            raise ToolError("scope_denied", "in-app calls carry no token scope")
    elif channel == "mcp":
        if scope not in AGENT_SCOPES:
            raise ToolError("scope_denied", "agent token scope required")
        if scope == "agent:read" and spec.risk != "read":
            raise ToolError("scope_denied", "agent:read may call read tools only")
    else:
        raise ToolError("scope_denied", f"unknown channel {channel!r}")


async def _gate_and_run(
    db: AsyncSession, user: User, name: str, raw_args: dict[str, Any] | None,
    channel: Channel, scope: str | None, api_token_id: int | None,
) -> Any:
    spec = _TOOLS.get(name)
    if spec is None:
        raise ToolError("unknown_tool", name)

    # 1. Args.
    try:
        args = spec.args.model_validate(raw_args or {})
    except PydanticValidationError as exc:
        raise ToolError(
            "invalid_arguments",
            "; ".join(f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors()),
        ) from None

    # Gate 6 (per-token rate limit, 561/578) goes ABOVE this line: a refused
    # call must not spend the plan's meter.
    await _admit_mcp_call(db, user, channel)

    await check_gates(db, user, spec, channel, scope)

    ctx = ToolContext(
        db=db, user=user, org_id=user.org_id, channel=channel, api_token_id=api_token_id
    )
    if spec.risk == "read":
        return await call_mapped(spec.run, ctx, args)
    from app.agent import actions  # deferred: actions imports this module

    return await actions.propose(ctx, spec, args, scope=scope)


async def _decide(
    which: str, db: AsyncSession, user: User, action_id: str, channel: Channel,
    scope: str | None, api_token_id: int | None,
) -> dict[str, Any]:
    from app.agent import actions  # deferred: actions imports this module

    org_id, user_id = user.org_id, user.id
    ctx = ToolContext(db=db, user=user, org_id=org_id, channel=channel, api_token_id=api_token_id)
    try:
        await _admit_mcp_call(db, user, channel)
        data = await getattr(actions, which)(ctx, action_id, scope=scope)
    except ToolError as exc:
        await logger.ainfo(
            f"agent.action.{which}", channel=channel, org_id=org_id, user_id=user_id,
            outcome=exc.code,
        )
        exc.data = wrap_untrusted(exc.data)
        raise
    except Exception:
        # Everything after the claim is handled inside the engine; this is a
        # failure before it (the database was unreachable, say).
        await logger.aexception(f"agent.action.{which}.failed", channel=channel, org_id=org_id)
        await db.rollback()
        raise ToolError("internal", "the action failed") from None
    await logger.ainfo(
        f"agent.action.{which}", channel=channel, org_id=org_id, user_id=user_id, outcome="ok",
    )
    return {"data": wrap_untrusted(data)}


async def confirm_action(
    db: AsyncSession, user: User, action_id: str, *,
    channel: Channel, scope: str | None, api_token_id: int | None,
) -> dict[str, Any]:
    """Confirm a staged action as its own principal. The action's tool and
    arguments are the STORED ones: this signature takes no arguments."""
    return await _decide("confirm", db, user, action_id, channel, scope, api_token_id)


async def cancel_action(
    db: AsyncSession, user: User, action_id: str, *,
    channel: Channel, scope: str | None, api_token_id: int | None,
) -> dict[str, Any]:
    return await _decide("cancel", db, user, action_id, channel, scope, api_token_id)
