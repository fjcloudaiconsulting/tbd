"""Rate limits move to MySQL (INFRA-121): real MySQL fences.

Run with RATE_LIMIT_MYSQL_URL=mysql+aiomysql://... (a disposable database).
"""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import rate_limit_db
from app.models.rate_limit import RateLimit

URL = os.environ.get("RATE_LIMIT_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="RATE_LIMIT_MYSQL_URL not set")


@pytest.fixture
def mysql_engine(monkeypatch):
    eng = rate_limit_db._build_engine(URL)
    RateLimit.__table__.create(eng, checkfirst=True)
    monkeypatch.setattr(rate_limit_db, "_engine", eng)
    yield eng
    eng.dispose()


def _run(n: int, fn):
    barrier = threading.Barrier(n)

    def work(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(work, range(n)))


def test_m1_concurrent_hits_are_exact(mysql_engine):
    key = f"m1:{uuid.uuid4()}"
    counts = _run(32, lambda _i: rate_limit_db.hit(key, 60))
    assert sorted(counts) == list(range(1, 33))
    key2 = f"m1b:{uuid.uuid4()}"
    _run(8, lambda _i: [rate_limit_db.hit(key2, 60) for _ in range(25)])
    assert rate_limit_db.get(key2) == 200
    rate_limit_db.clear(key)
    rate_limit_db.clear(key2)


def test_m2_window_reset_on_mysql(mysql_engine, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(rate_limit_db, "_clock", lambda: t[0])
    key = f"m2:{uuid.uuid4()}"
    assert [rate_limit_db.hit(key, 10) for _ in range(3)] == [1, 2, 3]
    t[0] = 1005.0
    assert rate_limit_db.hit(key, 10) == 4
    t[0] = 1009.999
    assert rate_limit_db.hit(key, 10) == 5
    t[0] = 1010.0
    assert rate_limit_db.hit(key, 10) == 1
    t[0] = 1019.999
    assert rate_limit_db.hit(key, 10) == 2
    rate_limit_db.clear(key)
