"""Migration 096 (TBD-584): delete the Ollama credentials before the type change.

Runs the real migration code on SQLite against hand-built tables: two orgs
whose only provider is Ollama (one routed through it by default, one per
feature, both with ledger rows), and an org with an OpenAI key and its own
routing and ledger rows that must come through untouched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_V = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _V / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m096 = _load("_m096", "096_ai_provider_varchar.py")


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        for ddl in (
            "CREATE TABLE org_ai_credentials (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, "
            "provider VARCHAR(32) NOT NULL, encrypted_api_key TEXT, encrypted_bearer_token TEXT)",
            "CREATE TABLE org_ai_default_routing (org_id INTEGER PRIMARY KEY, "
            "credential_id INTEGER NOT NULL, model VARCHAR(64) NOT NULL)",
            "CREATE TABLE org_ai_feature_routing (org_id INTEGER NOT NULL, feature_name "
            "VARCHAR(64) NOT NULL, credential_id INTEGER NOT NULL, model VARCHAR(64) NOT NULL, "
            "PRIMARY KEY (org_id, feature_name))",
            "CREATE TABLE ai_usage_ledger (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, "
            "credential_id INTEGER)",
        ):
            c.execute(text(ddl))
        yield c


def _seed(c) -> None:
    # Org 1: Ollama with a bearer token, its default route. Org 2: Ollama
    # without a key, a feature route. Org 3: OpenAI, both routes.
    c.execute(text(
        "INSERT INTO org_ai_credentials (id, org_id, provider, encrypted_api_key, "
        "encrypted_bearer_token) VALUES (1, 1, 'ollama', NULL, 'tok'), "
        "(2, 2, 'ollama', NULL, NULL), (3, 3, 'openai', 'key', NULL)"
    ))
    c.execute(text(
        "INSERT INTO org_ai_default_routing (org_id, credential_id, model) VALUES "
        "(1, 1, 'llama3'), (3, 3, 'gpt-4o-mini')"
    ))
    c.execute(text(
        "INSERT INTO org_ai_feature_routing (org_id, feature_name, credential_id, model) "
        "VALUES (2, 'chat', 2, 'llama3'), (3, 'chat', 3, 'gpt-4o')"
    ))
    for i, (org_id, cred_id) in enumerate(((1, 1), (1, 1), (1, 1), (2, 2), (3, 3)), start=1):
        c.execute(text(
            "INSERT INTO ai_usage_ledger (id, org_id, credential_id) VALUES (:i, :o, :c)"
        ), {"i": i, "o": org_id, "c": cred_id})


def _state(c) -> dict:
    q = lambda sql: set(c.execute(text(sql)).all())  # noqa: E731
    return {
        "credentials": q("SELECT id, org_id, provider FROM org_ai_credentials"),
        "default": q("SELECT org_id, credential_id FROM org_ai_default_routing"),
        "feature": q("SELECT org_id, credential_id FROM org_ai_feature_routing"),
        "ledger": q("SELECT id, org_id, credential_id FROM ai_usage_ledger"),
    }


_AFTER = {
    "credentials": {(3, 3, "openai")},
    "default": {(3, 3)},
    "feature": {(3, 3)},
    # The usage history stays; only the link to the deleted credential goes.
    "ledger": {(1, 1, None), (2, 1, None), (3, 1, None), (4, 2, None), (5, 3, 3)},
}


def test_deletes_every_ollama_credential_and_its_routing(conn):
    """FENCE. Kills a migration that keeps an Ollama row (and its encrypted
    token), leaves a routing row pointing at a deleted credential, deletes
    the usage history, or touches another provider's rows."""
    _seed(conn)
    assert m096.delete_retired_credentials(conn) == 2
    assert _state(conn) == _AFTER


def test_is_idempotent(conn):
    _seed(conn)
    m096.delete_retired_credentials(conn)
    assert m096.delete_retired_credentials(conn) == 0
    assert _state(conn) == _AFTER


def test_deletes_and_detaches_in_batches(conn, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(m096, "BATCH", 1)
    assert m096.delete_retired_credentials(conn) == 2
    assert _state(conn) == _AFTER


def test_upgrade_deletes_before_the_type_change(conn, monkeypatch):
    """The delete runs inside ``upgrade`` and before ``alter_column``, so the
    table copy never carries the Ollama rows."""
    _seed(conn)
    seen = []

    class _Op:
        @staticmethod
        def get_bind():
            return conn

        @staticmethod
        def alter_column(table, column, **kw):
            seen.append(conn.execute(text(
                "SELECT COUNT(*) FROM org_ai_credentials WHERE provider = 'ollama'"
            )).scalar_one())
            assert (table, column) == ("org_ai_credentials", "provider")

    monkeypatch.setattr(m096, "op", _Op)
    m096.upgrade()
    assert seen == [0]
    assert _state(conn) == _AFTER
