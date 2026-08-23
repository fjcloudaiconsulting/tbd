import asyncio
import os
import subprocess
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.config import settings as app_settings
from app import redis_client
from redis.exceptions import AuthenticationError as RedisAuthenticationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError as RedisNoPermissionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from app.database import engine
from app.logging import setup_logging
from app.rate_limit import limiter
from app.routers import account_types, accounts, admin, admin_ai_usage, admin_analytics, admin_announcements, admin_audit, admin_broadcasts, admin_features, admin_orgs, admin_rate_limit_overrides, admin_roles, admin_subscriptions, admin_users, ai_budget, ai_categorize, ai_forecast, ai_providers, ai_status, announcements, api_tokens, auth, budgets, categories, cc_cycle_payments, dashboard, feedback, forecast, forecast_plans, import_router, notifications, onboarding, org_data, org_members, orgs, plans, public_stats, recurring, reports, scenarios, security, settings, subscriptions, tags, transactions, users, webhooks
from app.routers import scheduler as scheduler_router
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.import_ofx_service import init_ofx_executor, shutdown_ofx_executor
from app.services.scheduler.loop import scheduler_loop

# Setup JSON logging early so uvicorn's loggers are captured
setup_logging()

logger = structlog.stdlib.get_logger()


# K8S-2 (L0.6): subscription backfill was previously run on every lifespan
# boot via `_backfill_subscriptions`. Under multi-replica (HPA) that races
# (every replica scanning + inserting on startup). It is now a one-shot
# alembic data migration (`043_backfill_subscriptions`) made idempotent
# via a per-row HAS_SUBSCRIPTION guard plus the UNIQUE(org_id) constraint
# on the subscriptions table. The same backfill function is also reachable
# as an ops utility at `backend/scripts/backfill_subscriptions.py` for
# manual recovery.


_ALEMBIC_INI_PATH = "/app/alembic.ini"


def _resolve_alembic_head() -> str:
    """Return the head revision recorded in the alembic versions tree.

    Uses the alembic Python API directly (no subprocess) so this stays
    cheap enough to call on every dev boot. Returns "unknown" if anything
    goes wrong; we never want diagnostic logging to gate startup.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(_ALEMBIC_INI_PATH)
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        if len(heads) == 1:
            return heads[0]
        # Multi-head or no heads: surface the raw shape rather than guess.
        return ",".join(heads) if heads else "unknown"
    except Exception:
        return "unknown"


async def _resolve_alembic_current() -> str:
    """Return the alembic_version row from the live DB.

    Direct SQL via the existing async engine, far cheaper than spinning
    up a separate alembic context. Returns "unknown" on any error so a
    log line is still emitted; the actual upgrade run will surface real
    failures. Returns "none" if alembic_version is empty (fresh DB).
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.first()
            if row is None:
                return "none"
            return str(row[0])
    except Exception:
        return "unknown"


_GIT_HEAD_PATH = "/app/.git/HEAD"


def _detect_branch() -> str | None:
    """Read the current branch directly from /app/.git/HEAD.

    docker-compose mounts the host repo's .git directory read-only into
    /app/.git, so this is a file read with no subprocess and no git
    binary required in the container. Returns:

      * the branch name, when HEAD is a symbolic ref (the normal case)
      * None, when HEAD is detached (a raw SHA), the file is missing,
        or anything else goes wrong (worktree gitdir indirection,
        permissions, etc.)

    Callers MUST treat None as "couldn't tell" - the lifespan guard
    refuses to migrate in that case so we fail closed, not open.
    """
    try:
        with open(_GIT_HEAD_PATH) as f:
            head = f.read().strip()
    except (OSError, ValueError):
        return None
    prefix = "ref: refs/heads/"
    if head.startswith(prefix):
        return head[len(prefix):] or None
    return None


def _resolve_git_branch() -> str:
    """String-returning wrapper around `_detect_branch()` for diagnostic
    logging. Returns "unknown" rather than None so structured log fields
    stay typed.
    """
    return _detect_branch() or "unknown"


def _migrate_off_main_override_set() -> bool:
    """True when the operator has opted in to lifespan migrations from
    a non-main checkout. Mirrors the CLI guard in `./pfv migrate`. Same
    env var name on purpose so a single export covers both surfaces.
    """
    return os.environ.get("PFV_MIGRATE_OK_OFF_MAIN", "").strip() == "1"


async def _run_migrations() -> None:
    """Run Alembic migrations on startup. Idempotent: alembic upgrade head
    is a no-op when already at the latest revision.

    Refuses to run when the host checkout is on a non-main branch unless
    `PFV_MIGRATE_OK_OFF_MAIN=1` is set. A migrate from a feature branch
    can leave alembic_version pointing at a revision that only exists on
    that branch, which then breaks the next `./pfv start` on main until
    the version row is hand-patched. Same drift class the 2026-05-09
    incident demonstrated. Detached HEAD / unreadable HEAD also refuses
    (fail closed). See
    ~/.claude/projects/-Users-fjorge-src-pfv/memory/reference_shared_mysql_volume_trap.md.

    Logs the resolved head + current revision (and best-effort git branch)
    before invoking alembic so the next drift incident has a breadcrumb
    pointing at exactly which revision the lifespan was targeting. Skips
    the subprocess entirely when current == head.
    """
    branch = _detect_branch()
    if branch != "main" and not _migrate_off_main_override_set():
        logger.error(
            "migrate.dev.refused",
            branch=branch if branch is not None else "unknown",
            reason=(
                "branch_not_main" if branch is not None else "branch_undetectable"
            ),
            override_env_var="PFV_MIGRATE_OK_OFF_MAIN",
        )
        raise RuntimeError(
            "Refusing to run dev lifespan migrations from non-main branch "
            f"({'detached/unknown' if branch is None else branch!r}). "
            "Set PFV_MIGRATE_OK_OFF_MAIN=1 in .env or the shell to override. "
            "See reference_shared_mysql_volume_trap.md."
        )

    head = _resolve_alembic_head()
    current = await _resolve_alembic_current()
    branch_for_log = branch or "unknown"

    if current == head and head != "unknown":
        logger.info(
            "migrate.dev.no_op",
            current_revision=current,
            head_revision=head,
            branch=branch_for_log,
        )
        return

    logger.info(
        "migrate.dev.target",
        current_revision=current,
        head_revision=head,
        branch=branch_for_log,
    )

    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Migration failed: {result.stderr}")


class AiCredentialKeyReusesMfaKey(RuntimeError):
    """Raised when ``AI_CREDENTIAL_ENCRYPTION_KEY`` shares its value with
    ``MFA_ENCRYPTION_KEY`` (or either of their _PREV rotations).

    The two KEKs guard distinct datasets (TOTP secrets vs. provider API
    keys) and MUST stay separated so a compromise of one envelope key
    doesn't unlock the other. The PR1 startup guard refuses to boot
    when the SHA-256 fingerprints collide.
    """


def _sha256_hex(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_ai_credential_kek_separation(settings_obj=app_settings) -> None:
    """Refuse to boot when the AI KEK matches the MFA KEK.

    Skipped in ``APP_ENV=test`` so the unit-test harness can run with
    a single fixture key. Do not re-add this check in tests — see #338
    spec. The production + development + staging paths all run it.
    """
    if settings_obj.app_env == "test":
        return
    ai_key = settings_obj.ai_credential_encryption_key
    if not ai_key:
        # Empty AI key: the encryption helper raises a clear error at
        # first use. No KEK collision possible with an empty key.
        return
    mfa_key = settings_obj.mfa_encryption_key
    ai_keys = {_sha256_hex(ai_key)}
    if settings_obj.ai_credential_encryption_key_prev:
        ai_keys.add(_sha256_hex(settings_obj.ai_credential_encryption_key_prev))
    mfa_keys: set[str] = set()
    if mfa_key:
        mfa_keys.add(_sha256_hex(mfa_key))
    # MFA_ENCRYPTION_KEY_PREV is not currently a setting; if added in
    # future, include it here. Today the MFA service only carries the
    # current key, so this set has at most one element.
    collision = ai_keys & mfa_keys
    if collision:
        logger.error(
            "config.ai_credential_key_reuses_mfa_key",
            message=(
                "AI_CREDENTIAL_ENCRYPTION_KEY must not equal MFA_ENCRYPTION_KEY. "
                "Generate a separate Fernet key for AI credentials."
            ),
        )
        raise AiCredentialKeyReusesMfaKey(
            "AI_CREDENTIAL_ENCRYPTION_KEY reuses MFA_ENCRYPTION_KEY value"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # PR1 of the AI tier train — refuse to boot if the AI KEK collides
    # with the MFA KEK (covers _PREV rotation slots too). See
    # ``verify_ai_credential_kek_separation`` docstring.
    verify_ai_credential_kek_separation()
    # Production runs migrations as a true init step (App Platform
    # PRE_DEPLOY job in .do/app.yaml; initContainer in k8s/templates/
    # backend.yaml) so they don't gate uvicorn's port-bind. Dev runs them
    # inline because the dev orchestrator (docker-compose) has no PRE_DEPLOY
    # equivalent. The alternative is a manual `./pfv migrate` after every
    # rebuild.
    if app_settings.app_env != "production":
        await _run_migrations()
    # NOTE: subscription backfill used to run here on every boot. It now
    # lives in alembic migration 043_backfill_subscriptions, idempotent
    # via a per-row HAS_SUBSCRIPTION existence check (plus the UNIQUE
    # constraint on subscriptions.org_id). Multi-replica safe. For manual
    # re-runs, see `backend/scripts/backfill_subscriptions.py`.
    await logger.ainfo("starting", app=app_settings.app_name, env=app_settings.app_env)
    # OFX statement imports parse in hard-killable child processes bounded by
    # a per-org + global concurrency cap. Create the process-local executor
    # here so the caps bind to this event loop; the child processes it owns
    # are per-request and reaped on completion / timeout.
    init_ofx_executor(app_settings)
    if app_settings.scheduler_enabled:
        app.state.scheduler_stop = asyncio.Event()
        app.state.scheduler_task = asyncio.create_task(
            scheduler_loop(
                app.state.scheduler_stop,
                tick_seconds=app_settings.scheduler_tick_seconds,
                lock_ttl=app_settings.scheduler_lock_ttl_seconds,
                max_orgs=app_settings.scheduler_max_orgs_per_tick,
            )
        )
        # Yield control once so the just-created task actually starts
        # running (up to its first internal suspension point) before we
        # hand control back to uvicorn. asyncio.create_task only
        # schedules the coroutine; without this checkpoint the task
        # would not get a turn until something else awaits.
        await asyncio.sleep(0)
    yield
    if app_settings.scheduler_enabled and getattr(app.state, "scheduler_task", None):
        app.state.scheduler_stop.set()
        try:
            await asyncio.wait_for(app.state.scheduler_task, timeout=10)
        except asyncio.TimeoutError:
            app.state.scheduler_task.cancel()
    shutdown_ofx_executor()
    await redis_client.close_client()
    await engine.dispose()
    await logger.ainfo("shutdown complete")


_is_dev = app_settings.app_env == "development"

app = FastAPI(
    title=app_settings.app_name,
    lifespan=lifespan,
    # Swagger UI moved under /api/ so the frontend can own /docs as the
    # public in-app user manual. The browser path is /api/docs (proxied
    # by nginx through the existing /api/* rule); FastAPI serves
    # /api/docs and /api/openapi.json directly.
    docs_url="/api/docs" if _is_dev else None,
    openapi_url="/api/openapi.json" if _is_dev else None,
    redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=app_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    expose_headers=["X-Request-Id"],
)

# L1.5: stamp baseline security headers (HSTS, X-Content-Type-Options,
# X-Frame-Options, Referrer-Policy, Permissions-Policy,
# Cross-Origin-Opener-Policy, Cross-Origin-Resource-Policy) on every
# HTTP response from the backend, matching frontend/next.config.ts.
# The DO App Platform ingress routes /api/*, /health, and /ready
# directly to the backend component, bypassing the frontend's Next.js
# header config — without this middleware those endpoints return
# headerless responses and the host fails HSTS preload checks.
#
# Placement detail: SecurityHeadersMiddleware MUST sit OUTSIDE
# Starlette's ServerErrorMiddleware so it can also stamp headers on
# 500 responses generated by the framework for exceptions that no
# @app.exception_handler caught. Starlette's normal middleware stack
# is built as
#     ServerErrorMiddleware -> user_middleware -> ExceptionMiddleware
# so anything registered via `app.add_middleware(...)` is INSIDE
# ServerErrorMiddleware and would miss those 500s. We instead wrap
# the result of `build_middleware_stack` so SecurityHeadersMiddleware
# becomes the truly outermost ASGI wrapper of this app. See the
# middleware module docstring + test_security_headers_on_unhandled_500.
_inner_build_middleware_stack = app.build_middleware_stack


def _build_middleware_stack_with_security_headers():
    return SecurityHeadersMiddleware(_inner_build_middleware_stack())


app.build_middleware_stack = _build_middleware_stack_with_security_headers  # type: ignore[method-assign]

# L4.9: bind a per-request correlation id (and clear any leftover
# structlog contextvars from a previous request) at the very edge of
# the stack. Added LAST so it sits OUTERMOST in the ASGI chain
# (Starlette adds middleware in reverse order) — guarantees the
# context is set before any other middleware logs a thing.
app.add_middleware(RequestContextMiddleware)

@app.exception_handler(NotFoundError)
async def not_found_handler(request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def validation_handler(request, exc: ValidationError):
    return JSONResponse(status_code=400, content={"detail": exc.detail})


@app.exception_handler(ConflictError)
async def conflict_handler(request, exc: ConflictError):
    content = {"detail": exc.detail}
    # Surface the optional machine-readable code so clients can branch on
    # the failure reason (e.g. forecast "mixed_granularity") without
    # parsing English.
    if getattr(exc, "code", None):
        content["code"] = exc.code
    return JSONResponse(status_code=409, content=content)


# Field names whose VALUES must never be echoed back in 422 validation
# errors. FastAPI's default RequestValidationError handler includes the
# raw input under `detail[i].input` — for body-level errors that's the
# whole submitted dict, for field-level errors it's just the offending
# scalar value. Both shapes can leak secrets; both are handled below.
#
# Match by exact key name. Adding more names is forward-compatible;
# removing any is a regression (test_sensitive_field_set_covers_review_required_names).
_SENSITIVE_FIELD_NAMES = frozenset({
    "password",
    "new_password",
    "current_password",
    "confirm_password",
    "token",
    "refresh_token",
    "mfa_token",
    "email_token",
    "recovery_code",
    # MFA/TOTP/email-verify/recovery flows all use the bare `code` field
    # (backend/app/schemas/auth.py: MfaEnableRequest, MfaVerifyRequest,
    # MfaRecoveryRequest, MfaEmailVerifyRequest). A field-level validation
    # error on those would echo the submitted code without this entry.
    # No `country_code` / `currency_code` exists in schemas today, so the
    # bare match has no false positives.
    "code",
    # Cloudflare Turnstile response token submitted by the register
    # form. The token is single-use at Cloudflare's end (300 s TTL) but
    # is still bearer-shaped data that should never round-trip into a
    # 422 response body.
    "captcha_token",
})

_REDACTED = "<redacted>"


def _redact_sensitive(value):
    """Walk a JSON-shaped value and replace any dict field whose key
    matches `_SENSITIVE_FIELD_NAMES` with the literal '<redacted>'.

    Returns a new structure; does not mutate the input. Non-dict, non-
    list values pass through unchanged — the *caller* is responsible for
    deciding whether a top-level scalar is sensitive (via `loc`-based
    redaction in the handler below).
    """
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k in _SENSITIVE_FIELD_NAMES else _redact_sensitive(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(v) for v in value]
    return value


def _loc_targets_sensitive_field(loc) -> bool:
    """True when any element of pydantic's `loc` tuple matches a known
    sensitive field name. Pydantic field-level errors put the offending
    value in `input` as a scalar and identify the field through `loc`
    — e.g. {"loc": ["body", "password"], "input": "short"}. The
    recursive dict walk in `_redact_sensitive` does not catch this
    shape, so the handler checks `loc` separately and redacts `input`
    outright when the path includes a sensitive name.
    """
    if not isinstance(loc, (list, tuple)):
        return False
    return any(
        isinstance(part, str) and part in _SENSITIVE_FIELD_NAMES
        for part in loc
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request, exc: RequestValidationError):
    """Sanitize FastAPI's default 422 response so we don't echo
    submitted passwords / tokens / codes back to the client (and into
    any 4xx response log capture). Preserves the standard
    `{detail: [...]}` shape — only `detail[i].input` is sanitized.

    Two shapes get redacted:
      1. Body-level errors with `input` = the full submitted dict —
         walked recursively, sensitive keys' values replaced.
      2. Field-level errors with `input` = the scalar value of the
         failing field, identified through `loc` (e.g. ["body",
         "password"]). The whole `input` is replaced with '<redacted>'.
    """
    redacted_errors = []
    for err in exc.errors():
        new_err = dict(err)
        if "input" in new_err:
            if _loc_targets_sensitive_field(new_err.get("loc")):
                new_err["input"] = _REDACTED
            else:
                new_err["input"] = _redact_sensitive(new_err["input"])
        redacted_errors.append(new_err)
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(redacted_errors)},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(onboarding.router)
app.include_router(account_types.router)
app.include_router(accounts.router)
app.include_router(cc_cycle_payments.router)
app.include_router(categories.router)
app.include_router(transactions.router)
app.include_router(recurring.router)
app.include_router(budgets.router)
app.include_router(forecast.router)
app.include_router(forecast_plans.router)
app.include_router(settings.router)
app.include_router(import_router.router)
app.include_router(subscriptions.router)
app.include_router(plans.router)
app.include_router(admin.router)
app.include_router(admin_features.router)
app.include_router(admin_orgs.router)
app.include_router(admin_audit.router)
app.include_router(admin_broadcasts.router)
app.include_router(api_tokens.router)
app.include_router(admin_analytics.router)
app.include_router(admin_roles.router)
app.include_router(admin_subscriptions.router)
app.include_router(admin_users.router)
app.include_router(org_members.router)
app.include_router(org_data.router)
app.include_router(orgs.router)
app.include_router(tags.router)
app.include_router(tags.transaction_tags_router)
app.include_router(feedback.router)
app.include_router(announcements.router)
app.include_router(admin_announcements.router)
app.include_router(notifications.router)
app.include_router(reports.router)
app.include_router(dashboard.router)
app.include_router(scenarios.router)
app.include_router(ai_providers.router)
app.include_router(ai_status.router)
app.include_router(ai_budget.router)
app.include_router(ai_categorize.router)
app.include_router(ai_forecast.router)
app.include_router(admin_ai_usage.router)
app.include_router(admin_rate_limit_overrides.router)
app.include_router(security.router)
app.include_router(public_stats.router)
app.include_router(scheduler_router.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Readiness probes (TBD-413) ──────────────────────────────────────────────
#
# TWO endpoints, deliberately, with different jobs.
#
# ``/ready`` is the ROTATION gate: "should traffic be sent to this instance".
# It checks the database and NOTHING else, and its response contract is
# frozen. ``k8s/templates/backend.yaml`` points a readinessProbe at it, and
# Redis is a single shared instance — so making a Redis failure non-200 here
# would fail every replica's readiness at once and evict the entire
# deployment, including the data plane, which does not need Redis at all
# (``app/deps.py`` has zero Redis references; access tokens live 15 minutes).
# ``.github/workflows/test.yml``'s ``Migration Checks`` also boots the app
# with no ``REDIS_URL`` and asserts this endpoint returns 200, and it feeds
# the REQUIRED ``Backend Checks`` gate.
#
# ``/health/dependencies`` is the TRUTH surface: per-dependency state, 503
# when a required dependency is unusable. That is what a monitor and a human
# read. On 2026-08-19 Redis was enforcing a stale password, every login
# returned 503, and ``/ready`` still said ``{"status":"ready"}`` — the whole
# reason this split exists.
#
# ⚠ SCOPE RULE for ``/health/dependencies``: every check on it is REQUIRED by
# construction. A dependency whose failure does not change the status code
# does not belong here. Without that rule it accretes Mailgun, then an AI
# provider, then object storage, until nobody trusts its 503.

# The database bound is the ONLY bound on the query, not belt-and-braces:
# per ``database.py`` aiomysql 0.2.0 accepts no ``read_timeout``, so
# ``connect_timeout`` covers connection ESTABLISHMENT only, a ``SELECT 1`` on
# an established-but-wedged socket has no driver bound at all, and
# ``pool_pre_ping=True`` adds another unbounded query on checkout. 3.0s
# rather than something tighter because a cold pool-grow connect legitimately
# takes seconds, and a false alarm on a deploy gate is the expensive failure.
_DB_PROBE_TIMEOUT_S = 3.0

# DERIVED, not picked, and deliberately NOT derived tightly.
#
#   documented worst case = 1.0 + 1 * (1.0 + 1.0 + 0.2) = 3.2s
#     socket_timeout 1.0 (first PING) + one retry of
#     (connect 1.0 + PING 1.0 + backoff cap 0.2)
#
# per ``redis_client.get_client()``'s own docstring. The infrastructure is
# documented to produce idle-dropped pooled sockets (App Platform NAT / VPC
# router, the 2026-05-19 trace), and ``_build_auth_redis_client``'s single
# retry exists to absorb exactly that. A bound BELOW 3.2s would cancel the
# probe mid-retry and report a healthy Redis as failing while every real login
# absorbed the same blip.
#
# ⚠ 3.2s is a bound on the LIBRARY's own waits, not on wall clock. This
# coroutine also shares an event loop with the concurrent database probe and
# with real traffic, so scheduling delay lands on top of it. 3.5s left only
# 0.3s of headroom, which makes the single most common real event — an
# idle-dropped socket, the one every login absorbs transparently — report
# ``timeout`` + 503 under load. ``scripts/smoke-test.sh`` turns that 503 into
# a FAILED DEPLOY, so the cost of being 1.5s slow on a genuinely dead Redis is
# far below the cost of flapping.
#
# 5.0 still fits under ``_DEPS_PROBE_TOTAL_TIMEOUT_S`` because the two probes
# run CONCURRENTLY under one ``gather``: the total is max(3.0, 5.0) = 5.0, not
# 3.0 + 5.0. ``test_f17b_probe_bounds_fit_under_the_backstop`` fences that.
_REDIS_PROBE_TIMEOUT_S = 5.0

# Pure backstop above both per-probe bounds. If it ever fires, something
# pathological happened outside the probes; it must still degrade into the
# normal body with the unfinished checks reported as "timeout" — never a 500,
# never an empty body.
_DEPS_PROBE_TOTAL_TIMEOUT_S = 6.0


# ── The CLOSED state vocabularies (TBD-413) ────────────────────────────────
#
# Declared HERE, in the module that produces them, so there is exactly one
# place a new state can be introduced. Two fences hold them closed in both
# directions: ``test_f16_every_produced_state_is_declared_and_every_declaration_is_reachable``
# drives every scenario the probes can take and compares the SET of answers
# against these names, and ``test_f16b_probes_return_no_undeclared_string_literal``
# parses this module's AST so the same holds on branches no scenario reaches.
# A new state string has to be added here before it can be returned, and the
# endpoint's public contract cannot widen silently.
_DB_STATES: frozenset[str] = frozenset({"ok", "timeout", "unreachable"})
_REDIS_STATES: frozenset[str] = frozenset(
    {
        "ok",
        "timeout",
        "auth_failed",
        "unreachable",
        "disabled",
        "not_configured",
    }
)
# The subset of ``_REDIS_STATES`` that does NOT make the endpoint unhealthy.
# ``disabled`` is a supported mode outside production; ``not_configured`` is
# the same observation IN production and is deliberately absent here.
_REDIS_HEALTHY_STATES: frozenset[str] = frozenset({"ok", "disabled"})
_STATUS_VALUES: frozenset[str] = frozenset({"ok", "unhealthy"})


async def _select_one() -> None:
    """One trivial round trip through the shared async engine.

    ``engine`` is read as a module global at call time on purpose: it is the
    same name ``/ready`` uses, which is what lets a test substitute a broken
    engine and have BOTH surfaces see it.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _probe_database() -> str:
    """``ok`` | ``timeout`` | ``unreachable``. Never raises.

    No ``auth_failed`` here, deliberately, and the asymmetry with the Redis
    probe is intentional: MySQL's 1045 is reachable only through
    ``exc.orig.args[0]``, two wrapper layers deep, which is too fragile to
    claim in a contract.
    """
    try:
        await asyncio.wait_for(_select_one(), _DB_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return "timeout"
    except Exception:  # noqa: BLE001 - a probe reports, it never raises
        return "unreachable"
    return "ok"


async def _probe_redis() -> str:
    """``ok`` | ``timeout`` | ``auth_failed`` | ``unreachable`` |
    ``disabled`` | ``not_configured``. Never raises.

    ``disabled`` and ``not_configured`` are the SAME observation — an empty
    ``redis_url`` — named by environment, so the body explains itself without
    the reader knowing ``app_env``. Outside production, running without Redis
    is a supported mode; in production it means nobody can log in.

    ⚠ Probes the SHARED singleton with a raw ``ping()``. Never
    ``Redis.from_url()`` (a pool per scrape of an unauthenticated endpoint,
    measuring a path production never takes), never ``require_client()``, and
    never a ``@_normalize_transport_errors``-wrapped helper — that decorator
    calls ``_retire_poisoned_client``, so a monitor scrape against a blipping
    Redis would tear down the pool real auth traffic is using.

    Cancellation safety (redis==5.2.1, verified 2026-08-22): both
    ``AbstractConnection.send_packed_command`` and ``.read_response`` carry
    ``except BaseException: await self.disconnect(nowait=True); raise``, and
    ``CancelledError`` is a ``BaseException`` — so a timed-out ping closes its
    socket before propagating and cannot leave a half-read reply in the pool.
    ``ConnectionPool.ensure_connection`` re-verifies with
    ``can_read_destructive()`` on every checkout as a second layer.
    ``tests/test_readiness_dependencies.py::test_f14_*`` fences that default.

    ⚠ EXCEPT ORDER IS LOAD-BEARING. ``AuthenticationError`` SUBCLASSES
    ``ConnectionError``, so catching ConnectionError first would collapse a
    credential incident into "unreachable" and send an operator hunting a
    network fault. The 2026-08-19 outage was a credential failure.

    ⚠ ``NoPermissionError`` sits with it, and does NOT fall out of the same
    subclassing. It is a ``ResponseError`` -> ``RedisError`` (asserted in
    ``test_f4d_*``, exercised end-to-end by ``test_f4c_*``), so without naming
    it explicitly a lost/narrowed Redis ACL
    grant reports ``unreachable`` — the same misdiagnosis F4 exists to
    prevent, one rung over: the credential is accepted, the command is not.
    """
    client = redis_client.get_client()
    if client is None:
        return (
            "not_configured"
            if app_settings.app_env == "production"
            else "disabled"
        )
    try:
        await asyncio.wait_for(client.ping(), _REDIS_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return "timeout"
    except (RedisAuthenticationError, RedisNoPermissionError):
        return "auth_failed"
    except RedisTimeoutError:
        return "timeout"
    except (RedisConnectionError, RedisError, OSError):
        return "unreachable"
    except Exception:  # noqa: BLE001 - incl. uvloop's bare RuntimeError, which
        # must not turn a monitoring endpoint into a 500
        return "unreachable"
    return "ok"


@app.get("/ready")
async def ready():
    """Rotation gate. Database only — see the block comment above.

    ⚠ The response contract is FROZEN: same body, same two status codes.
    Do NOT add a Redis check here; ``/health/dependencies`` is where that
    belongs, and ``test_f10_ready_is_unchanged_when_redis_is_down`` will go
    red if you try.

    The ``wait_for`` is the only bound on this query (see
    ``_DB_PROBE_TIMEOUT_S``); without it a wedged-but-established socket
    hangs the probe indefinitely. A timeout is an ``OSError`` subclass, so it
    lands in the same ``except`` and produces the same 503 body as before.
    """
    try:
        await asyncio.wait_for(_select_one(), _DB_PROBE_TIMEOUT_S)
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        # ⚠ ``asyncio.wait_for`` raises a BARE ``TimeoutError()``, so
        # ``str(e)`` is the empty string — on precisely the wedged-socket mode
        # the ``wait_for`` was added to catch, where this line is the only
        # discriminating detail an operator gets. Carry the class explicitly.
        logger.error(
            "readiness check failed",
            error=str(e) or type(e).__name__,
            error_class=type(e).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "connection error"},
        )


@app.get("/health/dependencies")
async def health_dependencies():
    """Per-dependency truth. 503 when a required dependency is unusable.

    200 iff ``database == "ok"`` AND ``redis in {"ok", "disabled"}``.

    Both probes ALWAYS run and are ALWAYS reported — never short-circuit on
    the first failure. Mid-incident, "is Redis also gone?" is precisely the
    question this endpoint exists to answer, and an early return erases it.

    ⚠ Coarse strings only. This endpoint is unauthenticated, so no exception
    text, hostname, port or driver message ever reaches the body.
    """
    results = await _gather_dependency_checks()
    database, redis_state = results
    healthy = database == "ok" and redis_state in _REDIS_HEALTHY_STATES
    checks = {"database": database, "redis": redis_state}
    if healthy:
        return {"status": "ok", "checks": checks}
    # Structured, and deliberately NOT silenced in logging.py: the access log
    # line carries only the 503, while this names which dependency failed and
    # in what state. Together they are the "when did Redis go away" timeline
    # that did not exist on 2026-08-19.
    logger.error(
        "readiness.dependencies.unhealthy",
        database=database,
        redis=redis_state,
    )
    return JSONResponse(
        status_code=503,
        content={"status": "unhealthy", "checks": checks},
    )


async def _gather_dependency_checks() -> tuple[str, str]:
    """Run both probes concurrently under a total backstop.

    ``return_exceptions=True`` so a bug in one probe cannot take out the
    other's already-computed answer; anything that is not a string is
    reported as ``unreachable`` rather than crashing the endpoint.
    """
    try:
        raw = await asyncio.wait_for(
            asyncio.gather(
                _probe_database(), _probe_redis(), return_exceptions=True
            ),
            _DEPS_PROBE_TOTAL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return "timeout", "timeout"
    return (
        raw[0] if isinstance(raw[0], str) else "unreachable",
        raw[1] if isinstance(raw[1], str) else "unreachable",
    )
