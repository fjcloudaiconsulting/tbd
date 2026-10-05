"""Rate-limit counters in MySQL: rate limits move to MySQL (INFRA-121).

The only place the SQL lives. Sync, short transactions; async callers go
through ``asyncio.to_thread(rate_limit_db.hit, ...)``. No exception is caught
here: a DB error propagates and the caller fails closed.
"""
from __future__ import annotations

import hashlib
import time

from limits.storage import Storage
from sqlalchemy import case, create_engine, delete, select, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.models.rate_limit import RateLimit

_t = RateLimit.__table__
_clock = time.time
# A row is purged only this long after expiry, so a concurrent hit that read
# `now` a moment earlier never loses its count at the window edge.
_PURGE_MARGIN = 60
_PURGE_BATCH = 100


def _build_engine(url: str | None = None) -> Engine:
    u = make_url(url or settings.database_url).set(query={})
    if u.drivername.startswith("sqlite"):
        return create_engine(u.set(drivername="sqlite"))
    return create_engine(
        u.set(drivername="mysql+pymysql"),
        connect_args={
            "connect_timeout": 2,
            "read_timeout": 2,
            "write_timeout": 2,
            # server-side lock wait ends with the client's
            "init_command": "SET SESSION innodb_lock_wait_timeout=1",
        },
        isolation_level="READ COMMITTED",
        pool_size=2,
        max_overflow=5,
        pool_timeout=2,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle,
    )


_engine = _build_engine()


def _kh(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _upsert_stmt(dialect_name: str, key_hash: str, now: float, expiry: float, amount: int):
    expired = _t.c.expires_at <= now
    hits = case((expired, amount), else_=_t.c.hits + amount)
    expires_at = case((expired, now + expiry), else_=_t.c.expires_at)
    values = {"key": key_hash, "hits": amount, "expires_at": now + expiry}
    if dialect_name == "mysql":
        # ordered: MySQL assigns left to right, hits must read the OLD expires_at
        return mysql.insert(_t).values(values).on_duplicate_key_update(
            [("hits", hits), ("expires_at", expires_at)]
        )
    return sqlite.insert(_t).values(values).on_conflict_do_update(
        index_elements=[_t.c.key], set_={"hits": hits, "expires_at": expires_at}
    )


def hit(key: str, expiry: float, amount: int = 1) -> int:
    now = _clock()
    kh = _kh(key)
    with _engine.begin() as c:
        c.execute(_upsert_stmt(c.dialect.name, kh, now, expiry, amount))
        count = c.execute(select(_t.c.hits).where(_t.c.key == kh)).scalar_one()
        stale = c.execute(
            select(_t.c.key)
            .where(_t.c.expires_at < now - _PURGE_MARGIN)
            .limit(_PURGE_BATCH)
            .with_for_update(skip_locked=True)
        ).scalars().all()
        if stale:
            c.execute(delete(_t).where(_t.c.key.in_(stale)))
    return int(count)


def get(key: str) -> int:
    with _engine.connect() as c:
        row = c.execute(
            select(_t.c.hits).where(_t.c.key == _kh(key), _t.c.expires_at > _clock())
        ).first()
    return int(row[0]) if row else 0


def get_expiry(key: str) -> float:
    now = _clock()
    with _engine.connect() as c:
        row = c.execute(select(_t.c.expires_at).where(_t.c.key == _kh(key))).first()
    return float(row[0]) if row else now


def clear(key: str) -> None:
    with _engine.begin() as c:
        c.execute(delete(_t).where(_t.c.key == _kh(key)))


def reset() -> int:
    with _engine.begin() as c:
        return c.execute(delete(_t)).rowcount


def check() -> bool:
    with _engine.connect() as c:
        c.execute(text("SELECT 1"))
    return True


class DbStorage(Storage):
    STORAGE_SCHEME = ["tbd-db"]
    base_exceptions = SQLAlchemyError

    def __init__(self, uri: str | None = None, **_):
        super().__init__()

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        return hit(key, expiry, amount)

    def get(self, key: str) -> int:
        return get(key)

    def get_expiry(self, key: str) -> float:
        return get_expiry(key)

    def clear(self, key: str) -> None:
        clear(key)

    def reset(self) -> int:
        return reset()

    def check(self) -> bool:
        return check()
