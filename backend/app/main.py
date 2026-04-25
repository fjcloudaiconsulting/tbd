import subprocess
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from app.routers import account_types, accounts, admin, auth, budgets, categories, forecast, forecast_plans, import_router, plans, recurring, settings, subscriptions, transactions, users
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
    allow_headers=["Authorization", "Content-Type"],
)

@app.exception_handler(NotFoundError)
async def not_found_handler(request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def validation_handler(request, exc: ValidationError):
    return JSONResponse(status_code=400, content={"detail": exc.detail})


@app.exception_handler(ConflictError)
async def conflict_handler(request, exc: ConflictError):
    return JSONResponse(status_code=409, content={"detail": exc.detail})


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
