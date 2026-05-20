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

    The socket-layer timeouts (``connect_timeout`` / ``read_timeout`` /
    ``write_timeout``) bound how long aiomysql will block on a dead
    socket before raising. Without them, a stale pooled connection
    whose peer-side has been silently dropped by the VPC NAT causes
    ``pool_pre_ping``'s ping to wait for the kernel TCP RTO (tens of
    seconds), which is the actual mechanism behind the silent 46 s
    /refresh handler hang.
    """
    args: dict = {
        "connect_timeout": settings.db_connect_timeout,
        "read_timeout": settings.db_read_timeout,
        "write_timeout": settings.db_write_timeout,
    }
    if ":25060" in settings.database_url:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        args["ssl"] = ctx
    return args


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


# The VPC NAT between App Platform and the data-plane droplet drops
# idle TCP after ~5 minutes; the server-side wait_timeout is much
# longer. Without recycle-before-NAT-drop the pool hands out
# half-open sockets and the next pre_ping blocks on a dead socket
# until the kernel TCP RTO fires (tens of seconds). The socket
# timeouts in connect_args bound that worst case, and pool_recycle
# brings connections back BEFORE NAT can drop them.
#
# Single-replica today: SQLAlchemy defaults (pool_size=5, max_overflow=10)
# are safe. Multi-replica future (HPA): each replica gets its own
# pool, so total concurrent DB connections = replicas * (pool_size +
# max_overflow). Sized for the data-plane droplet's max_connections
# cap; override DB_POOL_SIZE / DB_MAX_OVERFLOW env vars when scaling
# horizontally.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_build_connect_args(),
    pool_pre_ping=True,
    pool_recycle=settings.db_pool_recycle,
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
