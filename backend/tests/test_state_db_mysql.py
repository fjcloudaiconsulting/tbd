"""Sessions, single-use tokens and leases move to MySQL (INFRA-122): real
MySQL 8.4 fences (SQLite has no row locks, so it cannot show these races).

Run with RATE_LIMIT_MYSQL_URL=mysql+aiomysql://... (a disposable database).
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app import rate_limit_db, state_db as s
from app.models import Base

URL = os.environ.get("RATE_LIMIT_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="RATE_LIMIT_MYSQL_URL not set")
TABLES = ("auth_session_families", "auth_session_members", "used_tokens", "leases")
TTL = 3600


@pytest.fixture
def mysql_engine(monkeypatch):
    eng = rate_limit_db._build_engine(URL)
    Base.metadata.create_all(eng, tables=[Base.metadata.tables[t] for t in TABLES], checkfirst=True)
    monkeypatch.setattr(s, "_engine", eng)
    yield eng
    eng.dispose()


def _run(n: int, fn):
    barrier = threading.Barrier(n)

    def work(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(work, range(n)))


def _sid():
    return uuid.uuid4().hex


def _members(eng, sid):
    with eng.connect() as c:
        return set(c.execute(select(s._M.c.jti).where(s._M.c.sid == sid)).scalars())


def _age(eng, sid):
    """Every member of the family was created 31 s ago."""
    with eng.begin() as c:
        c.execute(s.update(s._M).where(s._M.c.sid == sid).values(created_at=s.db_now(-31)))


def _head(eng, sid):
    with eng.connect() as c:
        return c.execute(select(s._F.c.head_jti).where(s._F.c.sid == sid)).scalar()


@pytest.mark.parametrize("n", [2, 10])
def test_m1_parallel_refreshes_of_one_token_exactly_one_wins(mysql_engine, n):
    sid = _sid()
    old = f"{sid}old"
    s._issue(old, sid, 1, TTL)
    out = _run(n, lambda i: s._rotate(old, f"{sid}new{i}", sid, 1, TTL))
    assert sorted(out) == sorted([s.SESSION_ROTATE_OK] + [s.SESSION_ROTATE_ALREADY_ROTATED] * (n - 1))
    winner = f"{sid}new{out.index(s.SESSION_ROTATE_OK)}"
    assert _head(mysql_engine, sid) == winner
    assert _members(mysql_engine, sid) == {old, winner}
    assert s._grace(old)["successor_jti"] == winner


@pytest.fixture
def slow_guard(monkeypatch):
    """Hold every rotate/detect between its guard reads and its writes, so
    racers deterministically overlap (without the family lock they fork)."""
    real = s._member_seq

    def slow(*a):
        out = real(*a)
        time.sleep(0.3)
        return out

    monkeypatch.setattr(s, "_member_seq", slow)


def test_m1b_overlapping_refreshes_of_one_token_exactly_one_wins(mysql_engine, slow_guard):
    sid = _sid()
    s._issue(sid + "old", sid, 1, TTL)
    out = _run(2, lambda i: s._rotate(sid + "old", f"{sid}new{i}", sid, 1, TTL))
    assert sorted(out) == [s.SESSION_ROTATE_ALREADY_ROTATED, s.SESSION_ROTATE_OK]


def test_m2_parallel_reuse_detections_revoke_once(mysql_engine, slow_guard):
    sid = _sid()
    s._issue(sid + "a", sid, 1, TTL)
    s._rotate(sid + "a", sid + "b", sid, 1, TTL)
    _age(mysql_engine, sid)
    out = _run(2, lambda _i: s._detect_reuse_and_revoke(sid + "a", sid))
    assert sorted(out) == sorted([(s.SESSION_REUSE_REUSED, 2), (s.SESSION_REUSE_UNKNOWN,)])


def test_m3_rotate_vs_revoke_never_leaves_a_live_successor(mysql_engine):
    for i in range(20):
        sid = _sid()
        s._issue(sid + "a", sid, 1, TTL)
        rot, revoked = _run(
            2, lambda k: s._rotate(sid + "a", sid + "b", sid, 1, TTL) if k == 0 else s._revoke_family(sid)
        )
        assert rot in (s.SESSION_ROTATE_OK, s.SESSION_ROTATE_REVOKED)
        assert revoked == sorted({sid + "a", sid + "b"} if rot == s.SESSION_ROTATE_OK else {sid + "a"})
        assert _head(mysql_engine, sid) is None
        assert _members(mysql_engine, sid) == set()


def test_m4_parallel_claims_of_an_expired_token_exactly_one(mysql_engine):
    tok = uuid.uuid4().hex
    assert s._claim("m4", tok, 0) is True  # expires at once
    out = _run(8, lambda _i: s._claim("m4", tok, 60))
    assert out.count(True) == 1


def test_m5_parallel_leases_exactly_one_holder(mysql_engine):
    name = f"m5:{uuid.uuid4().hex}"
    out = _run(8, lambda _i: s._acquire_lease(name, 60))
    assert sum(h is not None for h in out) == 1
    with mysql_engine.begin() as c:
        c.execute(s.update(s._L).where(s._L.c.name == name).values(expires_at=s.db_now(-1)))
    out = _run(8, lambda _i: s._acquire_lease(name, 60))
    assert sum(h is not None for h in out) == 1


def test_m6_case_differing_jtis_are_distinct(mysql_engine):
    sid = _sid()
    s._issue(sid + "AbCd", sid, 1, TTL)
    assert s._validate(sid + "abcd") is None
    assert s._validate(sid + "AbCd") is not None


def test_m7_a_held_family_lock_fails_fast(mysql_engine):
    sid = _sid()
    s._issue(sid + "a", sid, 1, TTL)
    with mysql_engine.begin() as c:
        c.execute(text("SELECT sid FROM auth_session_families WHERE sid = :s FOR UPDATE"), {"s": sid})
        t0 = time.monotonic()
        with pytest.raises(OperationalError):
            s._rotate(sid + "a", sid + "b", sid, 1, TTL)
        assert time.monotonic() - t0 < 3


def test_m8_pre_burst_jti_replayed_after_a_fresh_rotation_is_reuse(mysql_engine):
    sid = _sid()
    s._issue(sid + "a", sid, 1, TTL)
    s._rotate(sid + "a", sid + "b", sid, 1, TTL)
    _age(mysql_engine, sid)
    s._rotate(sid + "b", sid + "c", sid, 1, TTL)
    assert s._detect_reuse_and_revoke(sid + "b", sid) == (s.SESSION_REUSE_GRACE,)
    assert s._detect_reuse_and_revoke(sid + "a", sid) == (s.SESSION_REUSE_REUSED, 3)


def test_m9_columns_are_fsp6_and_ascii_bin(mysql_engine):
    with mysql_engine.connect() as c:
        rows = c.execute(text(
            "SELECT table_name, column_name, datetime_precision, collation_name "
            "FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name IN ('auth_session_families','auth_session_members','used_tokens','leases')"
        )).all()
    for t, col, fsp, coll in rows:
        if col in ("expires_at", "grace_until"):
            assert fsp == 6, (t, col)
        if col in ("sid", "jti", "head_jti", "name", "holder", "scope"):
            assert coll == "ascii_bin", (t, col)


def test_m10_jti_collision_is_classified_on_mysql(mysql_engine):
    sid = _sid()
    s._issue(sid + "a", sid, 1, TTL)
    s._rotate(sid + "a", sid + "b", sid, 1, TTL)
    assert s._rotate(sid + "b", sid + "a", sid, 1, TTL) == s.SESSION_ROTATE_JTI_COLLISION
    assert _head(mysql_engine, sid) == sid + "b"
