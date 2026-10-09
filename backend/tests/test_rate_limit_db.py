"""Rate limits move to MySQL (INFRA-121): storage module fences (SQLite)."""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mysql

from app import rate_limit_db


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(rate_limit_db, "_clock", lambda: t[0])
    return t


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _stored_keys() -> set[str]:
    with rate_limit_db._engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT key FROM rate_limits"))}


def test_f1_window_resets_at_expiry(clock):
    """F1: counts accumulate inside the window, restart at expires_at."""
    assert [rate_limit_db.hit("k", 10) for _ in range(3)] == [1, 2, 3]
    clock[0] = 1005.0
    assert rate_limit_db.hit("k", 10) == 4  # must not slide the window
    clock[0] = 1009.999
    assert rate_limit_db.hit("k", 10) == 5
    clock[0] = 1010.0
    assert rate_limit_db.hit("k", 10) == 1
    clock[0] = 1019.999
    assert rate_limit_db.hit("k", 10) == 2
    clock[0] = 1020.0
    assert rate_limit_db.hit("k", 10) == 1


def test_f1_amount(clock):
    assert rate_limit_db.hit("a", 10, amount=5) == 5
    assert rate_limit_db.hit("a", 10, amount=2) == 7


def test_f2_mysql_assigns_hits_before_expires_at():
    """F2: MySQL assigns left to right; hits must read the OLD expires_at."""
    stmt = rate_limit_db._upsert_stmt("mysql", "h" * 64, 1000.0, 10, 1)
    sql = str(stmt.compile(dialect=mysql.dialect()))
    tail = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert tail.index("hits =") < tail.index("expires_at ="), sql


def test_f3_purge_margin(clock):
    """F3: rows expired > 60 s are purged; recent and live rows are not."""
    rate_limit_db.hit("old", 10)  # expires 1010
    clock[0] = 1040.0
    rate_limit_db.hit("recent", 10)  # expires 1050
    clock[0] = 1100.0  # cutoff 1040: old (1010) goes, recent (1050) stays
    assert rate_limit_db.hit("live", 1000) == 1
    assert _stored_keys() == {_hash("recent"), _hash("live")}
    assert rate_limit_db.hit("live", 1000) == 2
    assert rate_limit_db.hit("recent", 10) == 1  # expired row restarts


def test_f4_keys_are_hashed(clock):
    long_key, uni = "x" * 300, "ключ-é-日本"
    for k in (long_key, uni):
        assert rate_limit_db.hit(k, 10) == 1
        assert rate_limit_db.hit(k, 10) == 2
    stored = _stored_keys()
    assert stored == {_hash(long_key), _hash(uni)}
    assert all(len(k) == 64 for k in stored)
    assert long_key not in stored and uni not in stored


def test_f5_get_expiry_clear_reset_check(clock):
    assert rate_limit_db.get("k") == 0
    assert rate_limit_db.get_expiry("k") == 1000.0  # absent -> now
    rate_limit_db.hit("k", 10)
    rate_limit_db.hit("j", 10)
    assert rate_limit_db.get("k") == 1
    assert rate_limit_db.get_expiry("k") == 1010.0
    clock[0] = 1010.0
    assert rate_limit_db.get("k") == 0  # expired reads as zero
    clock[0] = 1000.0
    rate_limit_db.clear("k")
    assert rate_limit_db.get("k") == 0 and rate_limit_db.get("j") == 1
    assert rate_limit_db.reset() == 1
    assert rate_limit_db.get("j") == 0
    assert rate_limit_db.check() is True


def test_f5_db_storage_registered():
    from limits.storage import storage_from_string

    s = storage_from_string("tbd-db://")
    assert isinstance(s, rate_limit_db.DbStorage)
    assert s.incr("s", 10) == 1 and s.get("s") == 1


def test_engine_config_is_pinned(monkeypatch):
    seen = {}

    def fake(url, **kw):
        seen["url"], seen["kw"] = url, kw

    monkeypatch.setattr(rate_limit_db, "create_engine", fake)
    rate_limit_db._build_engine("mysql+aiomysql://u:p@h/db?charset=utf8mb4")
    assert seen["url"].drivername == "mysql+pymysql"
    assert dict(seen["url"].query) == {}
    kw = seen["kw"]
    assert kw["connect_args"] == {
        "connect_timeout": 2,
        "read_timeout": 2,
        "write_timeout": 2,
        "init_command": "SET SESSION innodb_lock_wait_timeout=1, time_zone='+00:00'",
    }
    assert kw["isolation_level"] == "READ COMMITTED"
    assert (kw["pool_size"], kw["max_overflow"], kw["pool_timeout"]) == (2, 5, 2)
    assert kw["pool_pre_ping"] is True
    assert kw["hide_parameters"] is True
