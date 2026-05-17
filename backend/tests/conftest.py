import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The app settings module validates JWT_SECRET_KEY at import time.
# Tests set a stable secret up front so importing app modules does not
# depend on an external .env file being present in the worktree.
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "test-jwt-secret-that-is-long-enough-for-pytest-1234567890",
)
os.environ.setdefault("APP_ENV", "development")

# Match the production logging.py suppression: ofxtools emits per-row INFO
# during OFX parses ("Converting <STMTTRN>"). For tests that parse the
# 10k-row fixture this distorts wall-clock timing AND floods captured
# log output. Apply the same WARNING floor at conftest import so it
# takes effect before any test session-level fixture imports parser
# modules.
logging.getLogger("ofxtools").setLevel(logging.WARNING)


# ── Fake in-process Redis (PR 2 — backend session model) ────────────────────
#
# After PR 2 of ``specs/2026-05-17-backend-session-model.md`` every refresh
# JWT issue path writes ``auth:session:{jti}`` and ``auth:session:by_sid:{sid}``
# to Redis BEFORE the cookie is set, and the validation chain probes the
# primary key. The spec is explicit (§7.1): Redis unreachable means 503,
# not silent success. Tests cannot rely on a real Redis being present in
# every runtime, so an in-process fake stands in.
#
# This fake is auto-installed by ``_autouse_fake_redis`` below so every
# test in the suite that exercises a session-issue path Just Works without
# needing to opt in. Tests that want to assert Redis-unreachable behaviour
# (the 503 path) overwrite ``redis_client.get_client`` themselves to
# return None, which takes precedence.


class _FakeRedisPipeline:
    """In-memory pipeline mirroring the redis.asyncio.client.Pipeline ops
    used by ``session_issue`` / ``session_rotate``."""

    def __init__(self, store: "_SharedFakeRedis"):
        self._store = store
        self._ops: list[tuple[str, tuple, dict]] = []

    def set(self, key, value, ex=None, nx=False, **kwargs):
        self._ops.append(("set", (key, value), {"ex": ex, "nx": nx, **kwargs}))
        return self

    def sadd(self, key, *members):
        self._ops.append(("sadd", (key, *members), {}))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", (key, ttl), {}))
        return self

    def delete(self, *keys):
        self._ops.append(("delete", keys, {}))
        return self

    async def execute(self):
        if self._store.abort_pipeline:
            from redis.exceptions import RedisError

            raise RedisError("simulated MULTI/EXEC abort")
        results = []
        for op, args, kwargs in self._ops:
            if op == "set":
                key, value = args
                self._store._kv[key] = value
            elif op == "sadd":
                key, *members = args
                self._store._sets[key].update(members)
            elif op == "expire":
                _ = args  # TTL not simulated; would require a clock fixture
            elif op == "delete":
                for k in args:
                    self._store._kv.pop(k, None)
                    self._store._sets.pop(k, None)
            results.append(True)
        return results


class _SharedFakeRedis:
    """Minimum-viable fake of ``redis.asyncio.Redis`` covering the
    SET / GET / SADD / EXPIRE / DELETE / pipeline / setex /  ops used
    across the app (auth-session keys + MFA email-jti nonces).
    """

    def __init__(self):
        self._kv: dict[str, Any] = {}
        self._sets: dict[str, set] = defaultdict(set)
        self.abort_pipeline = False

    # Plain KV
    async def get(self, key):
        return self._kv.get(key)

    async def set(self, key, value, ex=None, **kwargs):
        self._kv[key] = value
        return True

    async def setex(self, key, ttl, value):
        self._kv[key] = value
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._kv:
                del self._kv[k]
                n += 1
        return n

    async def exists(self, *keys):
        return sum(1 for k in keys if k in self._kv)

    # Sets
    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def sismember(self, key, member):
        return member in self._sets.get(key, set())

    # Pipelines
    def pipeline(self, transaction=True):
        return _FakeRedisPipeline(self)

    # Lifecycle
    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def _autouse_fake_redis(monkeypatch):
    """Install a fresh in-process fake Redis for every test.

    Tests that want to assert the Redis-unreachable contract overwrite
    ``redis_client.get_client`` themselves (``lambda: None``); pytest's
    fixture ordering means their ``monkeypatch.setattr`` runs AFTER this
    fixture, so the override wins.
    """
    from app import redis_client

    fake = _SharedFakeRedis()
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)
    monkeypatch.setattr(redis_client, "_client", fake, raising=False)
    yield fake


def issue_test_refresh_token(user_id: int, **kwargs) -> str:
    """Test helper: mint a refresh JWT AND seed its Redis row in the
    autouse fake. Replaces direct ``create_refresh_token(user_id)`` calls
    in pre-PR2 tests that didn't have to worry about Redis state.

    Returns the JWT string. Internal jti/sid are written to the fake
    Redis so the validation chain (which now probes Redis) accepts the
    token in subsequent requests.

    PR 2 contract: ``create_refresh_token`` now returns ``(token, jti, sid)``
    AND every issue path must paired with a Redis primary-key + family
    write. Tests that hand-mint a refresh JWT to bypass /login should
    use this helper rather than ``create_refresh_token`` directly.
    """
    from app import redis_client as _rc
    from app.security import create_refresh_token, refresh_cookie_max_age
    import json

    token, jti, sid = create_refresh_token(user_id, **kwargs)
    client = _rc.get_client()
    if client is None:
        return token
    # Synchronous write into the autouse fake's backing dicts so callers
    # do not have to ``await`` from non-async test bodies.
    key = f"auth:session:{jti}"
    family_key = f"auth:session:by_sid:{sid}"
    payload = json.dumps({"user_id": user_id, "sid": sid}, separators=(",", ":"))
    # The autouse fake stores under ``_kv`` and ``_sets``; real
    # redis.asyncio.Redis instances don't expose those. The helper
    # therefore is only useful in tests where ``_autouse_fake_redis``
    # has installed our fake — which is every test by default.
    if hasattr(client, "_kv"):
        client._kv[key] = payload
        client._sets[family_key].add(jti)
    return token
