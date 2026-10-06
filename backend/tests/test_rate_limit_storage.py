"""The slowapi limiter stores its counters in MySQL (INFRA-121)."""
from __future__ import annotations

from app import rate_limit, rate_limit_db
from app.config import settings


def test_limiter_uses_db_storage_with_or_without_redis_url(monkeypatch):
    for url in ("redis://example:6379/0", ""):
        monkeypatch.setattr(settings, "redis_url", url)
        storage = rate_limit._build_limiter()._storage
        assert isinstance(storage, rate_limit_db.DbStorage), type(storage).__name__


def test_module_level_limiter_built_at_import_time():
    assert rate_limit.limiter is not None
    assert rate_limit.limiter._key_func is rate_limit.rate_limit_key
