"""K8S-2: migration 043_backfill_subscriptions sentinel + idempotency.

The legacy ``_backfill_subscriptions`` ran on every backend boot.
Under HPA / multi-replica that races — every replica scans and
inserts on startup. The new one-shot path runs the same logic from
inside ``alembic upgrade head``. These tests pin two things:

  1. First invocation inserts a trial subscription for every org that
     lacks one, pointing at the configured default plan.
  2. Second invocation is a no-op (zero inserts). The double-check
     inside the loop is the portable "sentinel" — independent of any
     persisted flag.

We build a small SQLite schema that mirrors the
``organizations`` / ``plans`` / ``subscriptions`` tables for the
columns the migration touches. No live MySQL needed; the function
itself only relies on standard SQL.
"""
from __future__ import annotations

import datetime
import importlib.util
import pathlib
import sqlite3

import pytest
import sqlalchemy as sa


# Python 3.12 deprecated sqlite3's built-in date/datetime adapters. The
# migration under test binds ``datetime.date`` / ``datetime.datetime``
# objects (trial_start, trial_end, created_at, updated_at), which would
# otherwise trip the default-adapter DeprecationWarning. Register the
# explicit ISO-8601 adapters the 3.12 docs recommend so the stored values
# stay byte-for-byte identical (ISO strings the tests parse back via
# ``date.fromisoformat``).
sqlite3.register_adapter(datetime.date, lambda d: d.isoformat())
sqlite3.register_adapter(datetime.datetime, lambda d: d.isoformat())


_MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "043_backfill_subscriptions.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_043_backfill_subscriptions", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite engine with a minimal subscriptions schema.

    Only the columns the migration reads or writes are modeled. The
    real production schema (MySQL, defined by migrations 023, 028,
    032, ...) is wider but functionally equivalent for this function.
    """
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text(
            """
            CREATE TABLE plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """
        ))
        conn.execute(sa.text(
            """
            CREATE TABLE organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
            """
        ))
        conn.execute(sa.text(
            """
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id INTEGER NOT NULL UNIQUE,
                plan_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                billing_interval TEXT NOT NULL,
                trial_start DATE,
                trial_end DATE,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        ))
    return engine


def _seed_plans(conn) -> None:
    conn.execute(sa.text(
        "INSERT INTO plans (name, slug, is_active, sort_order) "
        "VALUES ('Free', 'free', 1, 0), ('Pro', 'pro', 1, 1)"
    ))


def _seed_orgs(conn, names: list[str]) -> list[int]:
    ids: list[int] = []
    for n in names:
        result = conn.execute(
            sa.text("INSERT INTO organizations (name) VALUES (:n)"),
            {"n": n},
        )
        ids.append(int(result.lastrowid))
    return ids


def _count_subs(conn) -> int:
    return int(conn.execute(sa.text("SELECT COUNT(*) FROM subscriptions")).scalar() or 0)


def test_backfill_inserts_trial_for_every_missing_org(sqlite_engine):
    """First run: every org without a subscription gets one with status
    'trialing' and a trial window anchored at today."""
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        _seed_plans(conn)
        org_ids = _seed_orgs(conn, ["alpha", "beta", "gamma"])

        summary = module.backfill_missing_subscriptions(conn)

        assert summary["considered"] == 3
        assert summary["inserted"] == 3
        assert summary["plan_slug"] == "pro"
        assert summary["trial_days"] == 14
        assert _count_subs(conn) == 3

        rows = conn.execute(sa.text(
            "SELECT org_id, status, billing_interval FROM subscriptions ORDER BY org_id"
        )).all()
        assert [r.org_id for r in rows] == sorted(org_ids)
        for r in rows:
            assert r.status == "trialing"
            assert r.billing_interval == "monthly"


def test_backfill_is_idempotent_second_run_inserts_zero(sqlite_engine):
    """Second run is the sentinel: zero inserts, summary reflects it.

    This is the multi-replica safety net — even if two replicas
    invoked the function concurrently, the per-row HAS_SUBSCRIPTION
    check + the UNIQUE constraint on subscriptions.org_id make it
    impossible to double-insert."""
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        _seed_plans(conn)
        _seed_orgs(conn, ["alpha", "beta"])

        first = module.backfill_missing_subscriptions(conn)
        assert first["inserted"] == 2
        assert _count_subs(conn) == 2

        second = module.backfill_missing_subscriptions(conn)
        assert second["considered"] == 0
        assert second["inserted"] == 0
        assert _count_subs(conn) == 2


def test_backfill_skips_orgs_that_already_have_a_subscription(sqlite_engine):
    """Mixed state: org with a pre-existing subscription stays
    untouched; only orgs missing one get filled."""
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        _seed_plans(conn)
        org_ids = _seed_orgs(conn, ["alpha", "beta", "gamma"])

        # Pre-seed a subscription for 'beta' with a non-default plan
        # and a non-trialing status so we can detect any clobber.
        conn.execute(sa.text(
            """
            INSERT INTO subscriptions (
                org_id, plan_id, status, billing_interval,
                created_at, updated_at
            ) VALUES (:org_id, 1, 'active', 'yearly', '2026-01-01', '2026-01-01')
            """
        ), {"org_id": org_ids[1]})

        summary = module.backfill_missing_subscriptions(conn)
        assert summary["considered"] == 2  # alpha + gamma
        assert summary["inserted"] == 2
        assert _count_subs(conn) == 3

        beta = conn.execute(sa.text(
            "SELECT status, billing_interval, plan_id FROM subscriptions "
            "WHERE org_id = :org_id"
        ), {"org_id": org_ids[1]}).first()
        assert beta.status == "active"
        assert beta.billing_interval == "yearly"
        assert beta.plan_id == 1


def test_backfill_falls_back_to_first_active_plan_when_slug_missing(
    sqlite_engine, monkeypatch
):
    """If DEFAULT_PLAN_SLUG points at a non-existent plan, the fallback
    picks the first active plan by sort_order. Mirrors
    subscription_service.get_default_plan."""
    monkeypatch.setenv("DEFAULT_PLAN_SLUG", "enterprise-xyz")
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        _seed_plans(conn)
        _seed_orgs(conn, ["alpha"])

        summary = module.backfill_missing_subscriptions(conn)
        # Free has sort_order 0 -> fallback picks Free's id (=1).
        assert summary["plan_slug"] == "enterprise-xyz"
        assert summary["plan_id"] == 1
        assert summary["inserted"] == 1


def test_backfill_raises_when_no_active_plan_anywhere(sqlite_engine):
    """No active Plan row -> hard failure with a pointer to migration
    023. Pre-launch we want this to fail the deploy loudly rather than
    produce orphan rows."""
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        # No plans seeded at all.
        _seed_orgs(conn, ["alpha"])

        with pytest.raises(RuntimeError, match="no active Plan row found"):
            module.backfill_missing_subscriptions(conn)


def test_backfill_honors_trial_duration_env(sqlite_engine, monkeypatch):
    """TRIAL_DURATION_DAYS env var overrides the default 14-day window."""
    monkeypatch.setenv("TRIAL_DURATION_DAYS", "30")
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        _seed_plans(conn)
        _seed_orgs(conn, ["alpha"])
        summary = module.backfill_missing_subscriptions(conn)
        assert summary["trial_days"] == 30

        row = conn.execute(sa.text(
            "SELECT trial_start, trial_end FROM subscriptions WHERE org_id = 1"
        )).first()
        import datetime
        start = datetime.date.fromisoformat(str(row.trial_start))
        end = datetime.date.fromisoformat(str(row.trial_end))
        assert (end - start).days == 30


def test_backfill_handles_invalid_trial_duration_env(sqlite_engine, monkeypatch):
    """Garbage in TRIAL_DURATION_DAYS falls back to 14 rather than crashing."""
    monkeypatch.setenv("TRIAL_DURATION_DAYS", "not-a-number")
    module = _load_migration_module()
    with sqlite_engine.begin() as conn:
        _seed_plans(conn)
        _seed_orgs(conn, ["alpha"])
        summary = module.backfill_missing_subscriptions(conn)
        assert summary["trial_days"] == 14
