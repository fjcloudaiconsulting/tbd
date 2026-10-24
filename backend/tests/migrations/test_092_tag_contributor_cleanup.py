"""Migration 092 (BE-18): withdraw stale shared tag dictionary contributions.

Runs the real migration code on SQLite against hand-built tables holding the
states the pre-BE-18 app left behind: a contribution whose tag was deleted or
renamed, contributions of orgs that stopped sharing (a value other than
``"true"``, or no setting row), and inflated ``usage_count`` values.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.services import tag_service

_V = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _V / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m092 = _load("_m092", "092_tag_contributor_cleanup.py")


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        for ddl in (
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, "
            "name_normalized VARCHAR(32) NOT NULL)",
            "CREATE TABLE tag_dictionary (id INTEGER PRIMARY KEY, name_normalized VARCHAR(32) "
            "NOT NULL UNIQUE, contributor_org_count INTEGER NOT NULL DEFAULT 0, usage_count "
            "INTEGER NOT NULL DEFAULT 0, is_seed BOOLEAN NOT NULL DEFAULT 0)",
            "CREATE TABLE tag_dictionary_contributors (id INTEGER PRIMARY KEY, dictionary_tag_id "
            "INTEGER NOT NULL, contributor_org_id INTEGER NOT NULL)",
            'CREATE TABLE org_settings (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, "key" '
            "VARCHAR(100) NOT NULL, value TEXT NOT NULL)",
        ):
            c.execute(text(ddl))
        yield c


def _seed(c) -> None:
    # Orgs 1 and 2 share; 3 turned it off; 4 stored "True" (not "true", so
    # the app reads it as off); 5 never had the setting.
    for org_id, value in ((1, "true"), (2, "true"), (3, "false"), (4, "True")):
        c.execute(text('INSERT INTO org_settings (org_id, "key", value) VALUES (:o, :k, :v)'),
                  {"o": org_id, "k": "share_tag_data", "v": value})
    # Dictionary: inflated usage on both; "gym" is a seed.
    c.execute(text("INSERT INTO tag_dictionary (id, name_normalized, contributor_org_count, "
                   "usage_count, is_seed) VALUES (1, 'insurance', 5, 40, 0), "
                   "(2, 'gym', 2, 9, 1), (3, 'untouched', 1, 1, 0)"))
    # Tags each org has today. Org 2 deleted (or renamed) its "gym".
    for org_id, name in ((1, "insurance"), (1, "gym"), (2, "insurance"), (3, "insurance"),
                         (4, "insurance"), (5, "insurance"), (2, "untouched")):
        c.execute(text("INSERT INTO tags (org_id, name_normalized) VALUES (:o, :n)"),
                  {"o": org_id, "n": name})
    rows = [(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (2, 1), (2, 2), (3, 2)]
    for i, (dict_id, org_id) in enumerate(rows, start=1):
        c.execute(text("INSERT INTO tag_dictionary_contributors (id, dictionary_tag_id, "
                       "contributor_org_id) VALUES (:i, :d, :o)"),
                  {"i": i, "d": dict_id, "o": org_id})


def _state(c) -> tuple[set, dict]:
    contributors = set(c.execute(text(
        "SELECT dictionary_tag_id, contributor_org_id FROM tag_dictionary_contributors"
    )).all())
    counts = {name: (n, u) for name, n, u in c.execute(text(
        "SELECT name_normalized, contributor_org_count, usage_count FROM tag_dictionary"
    ))}
    return contributors, counts


def test_cleanup_keeps_only_contributions_the_app_would_keep(conn):
    """FENCE. Kills a migration that keeps rows of deleted tags, keeps orgs
    that stopped sharing, reads "True" as sharing, or leaves the old counts."""
    _seed(conn)
    deleted, recounted = m092.cleanup(conn)
    contributors, counts = _state(conn)
    assert contributors == {(1, 1), (1, 2), (2, 1), (3, 2)}
    assert counts == {"insurance": (2, 2), "gym": (1, 1), "untouched": (1, 1)}
    # "untouched" already had the right counts, so it is not written.
    assert (deleted, recounted) == (4, 2)


def test_cleanup_is_idempotent(conn):
    _seed(conn)
    m092.cleanup(conn)
    before = _state(conn)
    assert m092.cleanup(conn) == (0, 0)
    assert _state(conn) == before


def test_cleanup_deletes_in_batches(conn, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(m092, "BATCH", 2)
    assert m092.cleanup(conn)[0] == 4
    assert _state(conn)[0] == {(1, 1), (1, 2), (2, 1), (3, 2)}


def test_upgrade_runs_the_cleanup_and_downgrade_writes_nothing(conn):
    _seed(conn)
    with Operations.context(MigrationContext.configure(conn)):
        m092.upgrade()
        after = _state(conn)
        m092.downgrade()
    assert after[0] == {(1, 1), (1, 2), (2, 1), (3, 2)}
    assert _state(conn) == after


def test_literals_match_the_app():
    """Migrations never import ``app``; pin the copied literals instead."""
    assert m092.SHARE_TAG_DATA_KEY == tag_service.SHARE_TAG_DATA_KEY
    assert tag_service.share_tag_data_value_enabled(m092.SHARING_VALUE) is True
    assert m092.revision == "092_tag_contributor_cleanup"
    assert m092.down_revision == "091_micro_usd_metering"
