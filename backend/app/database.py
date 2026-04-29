import ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


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


# DO's network layer silently drops idle TCP to managed MySQL after ~10 min,
# but the server-side wait_timeout is 8 hours — so without pre_ping the pool
# hands out dead sockets and the next query fails with "Lost connection
# during query" (error 2013). Recycle well below the network idle threshold.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_build_connect_args(),
    pool_pre_ping=True,
    pool_recycle=1800,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
