"""Migration 077 — loan system-type backfill logic (Slice 1).

Exercises the real ``backfill_loan_type`` / ``delete_unreferenced_loan_types``
helpers from migration 077 on an in-memory SQLite DB. The loan columns are
plain types (no native ENUM), so the backfill logic is SQLite-portable and can
be tested without the Alembic op context or a MySQL service. The DDL itself
(op.add_column x5, drop) is verified separately by the up/down/up run on the
MySQL stack.

Covers spec §3.2: idempotency (re-run no-op), the slug-only existence guard
(custom is_system=False loan not double-seeded), and the downgrade delete of
only UNREFERENCED system loan types.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "077_loan_account_type.py"
)
_spec = importlib.util.spec_from_file_location("_m077", _MIGRATION_PATH)
m077 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m077)


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        c.execute(text("CREATE TABLE organizations (id INTEGER PRIMARY KEY)"))
        c.execute(
            text(
                "CREATE TABLE account_types ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER, "
                "name VARCHAR(100), slug VARCHAR(50), is_system BOOLEAN)"
            )
        )
        c.execute(
            text(
                "CREATE TABLE accounts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, account_type_id INTEGER)"
            )
        )
        yield c


def _loan_types(conn, org_id):
    return conn.execute(
        text(
            "SELECT id, is_system FROM account_types "
            "WHERE org_id = :o AND slug = 'loan'"
        ),
        {"o": org_id},
    ).all()


def test_backfill_inserts_one_loan_per_org(conn):
    conn.execute(text("INSERT INTO organizations (id) VALUES (1), (2)"))
    inserted = m077.backfill_loan_type(conn)
    assert inserted == 2
    assert len(_loan_types(conn, 1)) == 1
    assert len(_loan_types(conn, 2)) == 1


def test_backfill_is_idempotent(conn):
    conn.execute(text("INSERT INTO organizations (id) VALUES (1), (2)"))
    m077.backfill_loan_type(conn)
    inserted_again = m077.backfill_loan_type(conn)
    assert inserted_again == 0  # no double-create (no UNIQUE(org_id, slug))
    assert len(_loan_types(conn, 1)) == 1
    assert len(_loan_types(conn, 2)) == 1


def test_backfill_skips_org_with_custom_loan_type(conn):
    conn.execute(text("INSERT INTO organizations (id) VALUES (1)"))
    # An org that hand-created a custom (is_system=False) loan type.
    conn.execute(
        text(
            "INSERT INTO account_types (org_id, name, slug, is_system) "
            "VALUES (1, 'My Loan', 'loan', 0)"
        )
    )
    inserted = m077.backfill_loan_type(conn)
    assert inserted == 0  # slug-guard: not double-seeded
    rows = _loan_types(conn, 1)
    assert len(rows) == 1
    assert rows[0].is_system == 0  # the custom row is untouched


def test_downgrade_deletes_only_unreferenced_system_loan_types(conn):
    conn.execute(text("INSERT INTO organizations (id) VALUES (1), (2), (3)"))
    m077.backfill_loan_type(conn)  # system loan for orgs 1, 2, 3
    # org 1's loan type is referenced by an account -> must survive downgrade.
    ref_id = _loan_types(conn, 1)[0].id
    conn.execute(
        text("INSERT INTO accounts (account_type_id) VALUES (:t)"), {"t": ref_id}
    )
    # org 3 has a custom (is_system=False) loan type too -> must survive.
    conn.execute(
        text(
            "INSERT INTO account_types (org_id, name, slug, is_system) "
            "VALUES (3, 'Custom', 'loan', 0)"
        )
    )

    m077.delete_unreferenced_loan_types(conn)

    # org 1: referenced system loan kept
    assert len(_loan_types(conn, 1)) == 1
    # org 2: unreferenced system loan deleted
    assert len(_loan_types(conn, 2)) == 0
    # org 3: system loan deleted, custom loan kept
    org3 = _loan_types(conn, 3)
    assert len(org3) == 1
    assert org3[0].is_system == 0
