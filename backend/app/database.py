from urllib.parse import urlparse

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


logger = structlog.stdlib.get_logger()


def _build_connect_args() -> dict:
    """Build connect_args for the async engine.

    Production MySQL is self-hosted on the data-plane droplet (:3306) and
    reached over the private VPC, so no TLS is configured here. The old
    DO-managed-DB (:25060) branch that built an unverified SSL context
    (CERT_NONE) was removed; if a managed/remote DB ever returns, add TLS
    back by loading the provider CA into an ssl.SSLContext with
    check_hostname=True / CERT_REQUIRED — never CERT_NONE.

    ``connect_timeout`` bounds how long aiomysql will block while
    establishing a new connection — important for cold-start and
    pool-grow paths where a network blip would otherwise hang the
    handler. Per-operation read/write timeouts are NOT set here:
    aiomysql 0.2.0 (the version pinned in requirements.txt) does
    not accept ``read_timeout`` / ``write_timeout`` kwargs — those
    were added in 0.2.1+. The stale-socket-hang class is therefore
    bounded at two other layers: ``pool_recycle`` (rotates pooled
    connections before the VPC NAT can drop them) and the
    route-local ``asyncio.wait_for`` on ``/auth/refresh``.
    """
    args: dict = {
        "connect_timeout": settings.db_connect_timeout,
    }
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
