import ssl
from urllib.parse import urlparse

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


logger = structlog.stdlib.get_logger()


def _build_connect_args() -> dict:
    """Build connect_args for the async engine.

    DO managed MySQL requires SSL on external connections. When the DATABASE_URL
    contains a DO-style host (port 25060), enable SSL with server verification
    disabled (DO uses self-signed certs with their own CA).
    """
    if ":25060" in settings.database_url:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ctx}
    return {}


def _safe_host(database_url: str) -> str:
    """Extract just the host portion of the DB URL for diagnostic logging.

    SQLAlchemy URLs can include credentials inline. We only ever log the
    hostname so credentials never reach structlog output. Returns
    "unknown" if parsing fails for any reason.
    """
    try:
        # urlparse needs a scheme it understands; SQLAlchemy uses
        # mysql+aiomysql:// which urlparse handles fine (scheme parsing
        # stops at "+"). Hostname extraction works either way.
        return urlparse(database_url).hostname or "unknown"
    except Exception:
        return "unknown"


# DO's network layer silently drops idle TCP to managed MySQL after ~10 min,
# but the server-side wait_timeout is 8 hours, so without pre_ping the pool
# hands out dead sockets and the next query fails with "Lost connection
# during query" (error 2013). Recycle well below the network idle threshold.
#
# Single-replica today: SQLAlchemy defaults (pool_size=5, max_overflow=10)
# are safe. Multi-replica future (HPA): each replica gets its own pool,
# so total concurrent DB connections = replicas * (pool_size +
# max_overflow). Sized for the managed-DB max_connections cap; override
# DB_POOL_SIZE / DB_MAX_OVERFLOW env vars when scaling horizontally.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_build_connect_args(),
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

# Diagnostic breadcrumb so an operator triaging a connection-cap incident
# can grep the deploy log for the actual pool settings rather than guess
# at env-var resolution. Host only; credentials are never logged.
logger.debug(
    "db.engine.configured",
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    host=_safe_host(settings.database_url),
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
