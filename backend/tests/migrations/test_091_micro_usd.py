"""Migration 091 (TBD-630): AI spend accumulators cents -> micro-USD.

Runs the real migration code on SQLite. ``convert_rows`` is the only place the
x10,000 lives, so it is exercised on seeded rows (up and down), and the whole
``upgrade()`` / ``downgrade()`` runs under ``Operations.context``. The MySQL
``CHANGE`` and ``DIV`` branches are proven by the transcript in the PR.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.auth.feature_catalog import PlanUsageLimits

_V = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _V / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m083 = _load("_m083", "083_modular_entitlements.py")
m091 = _load("_m091", "091_micro_usd_metering.py")

UP = lambda x: x * 10_000  # noqa: E731
FLOOR = lambda x: x // 10_000  # noqa: E731
CENTS, MICROS = "platform_ai.cents", "platform_ai.micros"


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        for ddl in (
            "CREATE TABLE ai_usage_ledger (id INTEGER PRIMARY KEY, est_cost_cents INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE platform_ai_spend (id INTEGER PRIMARY KEY, cents BIGINT NOT NULL DEFAULT 0)",
            "CREATE TABLE usage_counters (id INTEGER PRIMARY KEY, meter VARCHAR(40) NOT NULL, value BIGINT NOT NULL)",
            "CREATE TABLE org_limit_overrides (id INTEGER PRIMARY KEY, meter VARCHAR(40) NOT NULL, limit_value BIGINT)",
            "CREATE TABLE plans (id INTEGER PRIMARY KEY, usage_limits JSON)",
            "CREATE TABLE org_ai_credentials (id INTEGER PRIMARY KEY, platform_provider VARCHAR(20))",
        ):
            c.execute(text(ddl))
        yield c


def _plan(c, pid, limits):
    c.execute(text("INSERT INTO plans (id, usage_limits) VALUES (:i, :v)"),
              {"i": pid, "v": json.dumps(limits)})


# ---- 14 ------------------------------------------------------------------

def test_fence_convert_usage_limits_up_and_down():
    """FENCE 14. Kills ceil on a limit downgrade (permissive) and dropping
    other meters."""
    d = {CENTS: {"period": "month", "limit": 3}, "mcp.calls": {"period": "month", "limit": None}}
    assert m091.convert_usage_limits(d, CENTS, MICROS, UP) == {
        MICROS: {"period": "month", "limit": 30_000},
        "mcp.calls": {"period": "month", "limit": None},
    }
    assert m091.convert_usage_limits({}, CENTS, MICROS, UP) is None
    with pytest.raises(RuntimeError):
        m091.convert_usage_limits({CENTS: {"limit": 1}, MICROS: {"limit": 1}}, CENTS, MICROS, UP)
    for before, want in ((15_001, 1), (9_999, 0), (0, 0), (None, None)):
        out = m091.convert_usage_limits({MICROS: {"period": "month", "limit": before}}, MICROS, CENTS, FLOOR)
        assert out == {CENTS: {"period": "month", "limit": want}}


# ---- 15 ------------------------------------------------------------------

def test_fence_check_preconditions(conn):
    """FENCE 15. Kills converting while a reservation could be in flight, and
    writing a limit every entitlement read would reject."""
    _plan(conn, 1, {CENTS: {"period": "month", "limit": 0}})
    # A BYOK credential (platform_provider NULL) must not trip the refusal.
    conn.execute(text("INSERT INTO org_ai_credentials (platform_provider) VALUES (NULL)"))
    assert m091.check_preconditions(conn) == [(1, {MICROS: {"period": "month", "limit": 0}})]

    big = 2**53 // 10_000 + 1
    conn.execute(text("UPDATE plans SET usage_limits = :v"),
                 {"v": json.dumps({CENTS: {"period": "month", "limit": big}})})
    with pytest.raises(RuntimeError):
        m091.check_preconditions(conn)

    conn.execute(text("UPDATE plans SET usage_limits = :v"),
                 {"v": json.dumps({CENTS: {"period": "month", "limit": 0}})})
    conn.execute(text("INSERT INTO org_limit_overrides (meter, limit_value) VALUES (:m, :v)"),
                 {"m": CENTS, "v": big})
    with pytest.raises(RuntimeError):
        m091.check_preconditions(conn)
    conn.execute(text("DELETE FROM org_limit_overrides"))

    conn.execute(text("INSERT INTO org_ai_credentials (platform_provider) VALUES ('openrouter')"))
    with pytest.raises(RuntimeError):
        m091.check_preconditions(conn)


# ---- 16 ------------------------------------------------------------------

def test_fence_083_literal_converted_equals_todays_catalog_default():
    """FENCE 16. Kills a catalog rename the migration does not mirror."""
    assert m091.convert_usage_limits(
        m083.CANONICAL_USAGE_LIMITS, CENTS, MICROS, UP
    ) == PlanUsageLimits().model_dump(by_alias=True)


# ---- 17 ------------------------------------------------------------------

def test_fence_convert_rows_up_and_down(conn):
    """FENCE 17. Kills a migration that renames ``est_cost_cents`` but drops
    ``* 10000`` ($50 of BYOK spend reads as 5,000 micros), converting other
    meters, ceil on limits, floor on spend."""
    # The state the renames leave behind: new column names, old values.
    conn.execute(text("ALTER TABLE ai_usage_ledger RENAME COLUMN est_cost_cents TO cost_micros"))
    conn.execute(text("ALTER TABLE platform_ai_spend RENAME COLUMN cents TO micros"))
    conn.execute(text("INSERT INTO ai_usage_ledger (id, cost_micros) VALUES (1, 5000)"))
    conn.execute(text("INSERT INTO platform_ai_spend (id, micros) VALUES (1, 3)"))
    conn.execute(text("INSERT INTO usage_counters (id, meter, value) VALUES (1, :m, 7), (2, 'mcp.calls', 7)"),
                 {"m": CENTS})
    conn.execute(text("INSERT INTO org_limit_overrides (id, meter, limit_value) VALUES (1, :m, 2)"), {"m": CENTS})
    _plan(conn, 1, {CENTS: {"period": "month", "limit": 5}, "mcp.calls": {"period": "month", "limit": 9}})

    m091.convert_rows(conn, True, m091.check_preconditions(conn))

    def snap():
        return (
            conn.execute(text("SELECT cost_micros FROM ai_usage_ledger")).scalar(),
            conn.execute(text("SELECT micros FROM platform_ai_spend")).scalar(),
            dict(conn.execute(text("SELECT meter, value FROM usage_counters")).all()),
            conn.execute(text("SELECT meter, limit_value FROM org_limit_overrides")).one(),
            json.loads(conn.execute(text("SELECT usage_limits FROM plans")).scalar()),
        )

    assert snap() == (
        50_000_000, 30_000, {MICROS: 70_000, "mcp.calls": 7}, (MICROS, 20_000),
        {MICROS: {"period": "month", "limit": 50_000}, "mcp.calls": {"period": "month", "limit": 9}},
    )

    conn.execute(text("UPDATE ai_usage_ledger SET cost_micros = 15001"))
    conn.execute(text("UPDATE platform_ai_spend SET micros = 1"))
    conn.execute(text("UPDATE usage_counters SET value = 10001 WHERE meter = :m"), {"m": MICROS})
    conn.execute(text("UPDATE org_limit_overrides SET limit_value = 15001"))
    conn.execute(text("UPDATE plans SET usage_limits = :v"),
                 {"v": json.dumps({MICROS: {"period": "month", "limit": 15_001}, "mcp.calls": {"period": "month", "limit": 9}})})

    m091.convert_rows(conn, False, m091.down_plan_rows(conn))

    assert snap() == (
        2, 1, {CENTS: 2, "mcp.calls": 7}, (CENTS, 1),
        {CENTS: {"period": "month", "limit": 1}, "mcp.calls": {"period": "month", "limit": 9}},
    )


# ---- 18 ------------------------------------------------------------------

def _run(conn, fn):
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def _col(conn, table, name):
    return next(c for c in inspect(conn).get_columns(table) if c["name"] == name)


def test_fence_whole_migration_up_and_down(conn):
    """FENCE 18. Kills ``upgrade()`` not calling ``convert_rows``, a plain
    ``alter_column(type_=)``, and a downgrade that renames before dividing. SQLite batch reflection keeps
    NOT NULL and the default even when ``existing_*`` is omitted, so a rename
    that drops them is NOT visible here; the MySQL ``SHOW CREATE TABLE``
    transcript in the PR is the proof for that."""
    conn.execute(text("INSERT INTO ai_usage_ledger (id, est_cost_cents) VALUES (1, 5000)"))
    conn.execute(text("INSERT INTO platform_ai_spend (id, cents) VALUES (1, 3)"))
    _plan(conn, 1, {CENTS: {"period": "month", "limit": 5}})

    _run(conn, m091.upgrade)

    names = lambda t: {c["name"] for c in inspect(conn).get_columns(t)}  # noqa: E731
    assert "est_cost_cents" not in names("ai_usage_ledger") and "cents" not in names("platform_ai_spend")
    led, sp = _col(conn, "ai_usage_ledger", "cost_micros"), _col(conn, "platform_ai_spend", "micros")
    assert "BIGINT" in str(led["type"]).upper() and led["nullable"] is False
    assert sp["nullable"] is False
    for c in (led, sp):
        assert str(c["default"]).strip("'") == "0"
    assert conn.execute(text("SELECT cost_micros FROM ai_usage_ledger")).scalar() == 50_000_000
    assert conn.execute(text("SELECT micros FROM platform_ai_spend")).scalar() == 30_000
    assert json.loads(conn.execute(text("SELECT usage_limits FROM plans")).scalar()) == {
        MICROS: {"period": "month", "limit": 50_000}}

    _run(conn, m091.downgrade)

    back = _col(conn, "ai_usage_ledger", "est_cost_cents")
    assert "INT" in str(back["type"]).upper() and "BIG" not in str(back["type"]).upper()
    assert back["nullable"] is False and "cents" in names("platform_ai_spend")
    assert conn.execute(text("SELECT est_cost_cents FROM ai_usage_ledger")).scalar() == 5000
    assert conn.execute(text("SELECT cents FROM platform_ai_spend")).scalar() == 3
    assert json.loads(conn.execute(text("SELECT usage_limits FROM plans")).scalar()) == {
        CENTS: {"period": "month", "limit": 5}}
