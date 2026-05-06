import subprocess
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.middleware.request_context import RequestContextMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select, text

from app.config import settings as app_settings
from app import redis_client
from app.database import async_session, engine
from app.models.subscription import Subscription
from app.models.user import Organization
from app.services import subscription_service
from app.logging import setup_logging
from app.rate_limit import limiter
from app.routers import account_types, accounts, admin, admin_audit, admin_orgs, auth, budgets, categories, forecast, forecast_plans, import_router, org_data, org_members, plans, recurring, settings, subscriptions, transactions, users
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

# Setup JSON logging early so uvicorn's loggers are captured
setup_logging()

logger = structlog.stdlib.get_logger()


async def _backfill_subscriptions() -> None:
    """Create trial subscriptions for any orgs that don't have one yet."""
    async with async_session() as db:
        result = await db.execute(
            select(Organization.id).where(
                ~Organization.id.in_(select(Subscription.org_id))
            )
        )
        org_ids = [row[0] for row in result.all()]
        for org_id in org_ids:
            await subscription_service.create_trial(db, org_id)
        if org_ids:
            await db.commit()
            await logger.ainfo("backfilled subscriptions", count=len(org_ids))


def _run_migrations() -> None:
    """Run Alembic migrations on startup. Idempotent — alembic upgrade head
    is a no-op when already at the latest revision."""
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Migration failed: {result.stderr}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production runs migrations as a true init step (App Platform
    # PRE_DEPLOY job in .do/app.yaml; initContainer in k8s/templates/
    # backend.yaml) so they don't gate uvicorn's port-bind. Dev runs them
    # inline because the dev orchestrator (docker-compose) has no PRE_DEPLOY
    # equivalent — the alternative is a manual `./pfv migrate` after every
    # rebuild.
    if app_settings.app_env != "production":
        _run_migrations()
    await _backfill_subscriptions()
    await logger.ainfo("starting", app=app_settings.app_name, env=app_settings.app_env)
    yield
    await redis_client.close_client()
    await engine.dispose()
    await logger.ainfo("shutdown complete")


_is_dev = app_settings.app_env == "development"

app = FastAPI(
    title=app_settings.app_name,
    lifespan=lifespan,
    docs_url="/docs" if _is_dev else None,
    openapi_url="/openapi.json" if _is_dev else None,
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
    return JSONResponse(status_code=409, content={"detail": exc.detail})


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
app.include_router(account_types.router)
app.include_router(accounts.router)
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
app.include_router(admin_orgs.router)
app.include_router(admin_audit.router)
app.include_router(org_members.router)
app.include_router(org_data.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        logger.error("readiness check failed", error=str(e))
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "connection error"},
        )
