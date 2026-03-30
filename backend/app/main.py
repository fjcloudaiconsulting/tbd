import subprocess
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.logging import setup_logging
from app.routers import auth

logger = structlog.stdlib.get_logger()


def _run_migrations() -> None:
    """Run Alembic migrations. In development, this runs on app startup.
    In production, use a K8s init container instead."""
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Migration failed: {result.stderr}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    _run_migrations()
    await logger.ainfo("starting", app=settings.app_name, env=settings.app_env)
    yield
    await engine.dispose()
    await logger.ainfo("shutdown complete")


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url="/docs" if settings.app_env == "development" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)


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
        return {"status": "not_ready", "database": str(e)}
