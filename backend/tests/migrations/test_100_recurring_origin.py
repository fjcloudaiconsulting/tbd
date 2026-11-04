"""Migration 100 (TBD-789): recurring transfer legs keep their template id.

Runs the real backfill on SQLite: only linked legs that still carry a
``recurring_id`` get it copied, across more than one batch, and a second run
writes nothing.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_V = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_spec = importlib.util.spec_from_file_location("_m100", _V / "100_recurring_origin.py")
m100 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m100)


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        c.execute(text(
            "CREATE TABLE transactions (id INTEGER PRIMARY KEY, recurring_id INTEGER, "
            "linked_transaction_id INTEGER, recurring_origin_id INTEGER)"
        ))
        yield c


def test_backfill_copies_the_template_onto_linked_recurring_legs_only(conn, monkeypatch):
    monkeypatch.setattr(m100, "BATCH", 2)
    rows = [
        (1, 7, 2), (2, 7, 1), (3, 8, 4), (4, 8, 3), (5, 9, 6),  # transfer legs
        (6, None, 5),  # a manual transfer leg
        (7, 9, None),  # a plain recurring expense
        (8, None, None),
    ]
    for i, rid, link in rows:
        conn.execute(text("INSERT INTO transactions (id, recurring_id, linked_transaction_id) "
                          "VALUES (:i, :r, :l)"), {"i": i, "r": rid, "l": link})
    assert m100.backfill(conn) == 5
    got = dict(conn.execute(text("SELECT id, recurring_origin_id FROM transactions")).all())
    assert got == {1: 7, 2: 7, 3: 8, 4: 8, 5: 9, 6: None, 7: None, 8: None}
    assert m100.backfill(conn) == 0
