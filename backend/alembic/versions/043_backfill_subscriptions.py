"""One-shot subscription backfill for orgs missing a trial.

Revision ID: 043_backfill_subscriptions
Revises: 042_users_onboarded_at
Create Date: 2026-05-13

K8S-2 (L0.6 multi-replica readiness): the legacy
``_backfill_subscriptions()`` call in the FastAPI lifespan
(``backend/app/main.py``) ran on every backend boot. Single-replica
that was harmless; under HPA / multi-replica each replica would race
to scan and insert. This migration lifts the backfill into a one-shot
data step, executed exactly once by the standard
``alembic upgrade head`` path that already runs on deploy (App
Platform PRE_DEPLOY job, K8s init container, ``docker-compose.prod
.yml migrate`` service, ``./pfv migrate``).

What it does:
  1. Reads the default plan slug + trial duration from the same env
     vars the runtime service uses (``DEFAULT_PLAN_SLUG``,
     ``TRIAL_DURATION_DAYS``) so the migration cannot drift from
     ``backend/app/services/subscription_service.create_trial``.
  2. For every ``organizations.id`` missing a ``subscriptions`` row,
     inserts a trial-status subscription pointing at the default plan
     with ``trial_start = today``, ``trial_end = today +
     trial_duration_days``.
  3. Acts as its own sentinel via the standard
     ``WHERE NOT EXISTS (...)`` insert pattern: rerunning the
     migration body (e.g. via the CLI utility at
     ``backend/scripts/backfill_subscriptions.py``) inserts zero new
     rows once every org has a subscription. Alembic's own
     ``alembic_version`` row is the durable "ran exactly once on this
     DB" sentinel.
  4. Emits a structured ``migrate.backfill.subscriptions.summary``
     line so the deploy log records how many rows were created (zero
     on already-migrated DBs is the expected steady state).

Down-migration: no-op. Removing seeded trial subscriptions would
strand existing users without billing context; pre-launch we hard-
remove freely but in this case the data is the new floor.
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import sqlalchemy as sa
from alembic import op


revision = "043_backfill_subscriptions"
down_revision = "042_users_onboarded_at"
branch_labels = None
depends_on = None


_DEFAULT_PLAN_SLUG_ENV = "DEFAULT_PLAN_SLUG"
_TRIAL_DURATION_DAYS_ENV = "TRIAL_DURATION_DAYS"

# Match Settings defaults in backend/app/config.py — if those defaults
# move, update both sides. The migration imports nothing from the live
# app module so this duplication is intentional (alembic best practice:
# data migrations are self-contained).
_FALLBACK_PLAN_SLUG = "pro"
_FALLBACK_TRIAL_DAYS = 14


def _resolved_plan_slug() -> str:
    return os.environ.get(_DEFAULT_PLAN_SLUG_ENV, _FALLBACK_PLAN_SLUG)


def _resolved_trial_days() -> int:
    raw = os.environ.get(_TRIAL_DURATION_DAYS_ENV, "")
    if not raw:
        return _FALLBACK_TRIAL_DAYS
    try:
        return int(raw)
    except ValueError:
        return _FALLBACK_TRIAL_DAYS


_LOOKUP_PLAN = sa.text(
    """
    SELECT id
      FROM plans
     WHERE slug = :slug
       AND is_active = TRUE
     LIMIT 1
    """
)

_LOOKUP_FALLBACK_PLAN = sa.text(
    """
    SELECT id
      FROM plans
     WHERE is_active = TRUE
     ORDER BY sort_order
     LIMIT 1
    """
)

_ORGS_WITHOUT_SUBSCRIPTION = sa.text(
    """
    SELECT o.id
      FROM organizations o
     LEFT JOIN subscriptions s ON s.org_id = o.id
     WHERE s.id IS NULL
    """
)

_HAS_SUBSCRIPTION = sa.text(
    "SELECT 1 FROM subscriptions WHERE org_id = :org_id LIMIT 1"
)

_INSERT_TRIAL = sa.text(
    """
    INSERT INTO subscriptions (
        org_id, plan_id, status, billing_interval,
        trial_start, trial_end,
        created_at, updated_at
    )
    VALUES (
        :org_id, :plan_id, 'trialing', 'monthly',
        :trial_start, :trial_end,
        :now, :now
    )
    """
)


def backfill_missing_subscriptions(bind) -> dict:
    """Insert a trial Subscription for every Organization that lacks one.

    Returns a summary dict ``{"considered": int, "inserted": int,
    "plan_slug": str, "plan_id": int|None, "trial_days": int}``.

    Idempotent: the ``NOT EXISTS`` clause in ``_INSERT_TRIAL`` guards
    against double-insertion even though the unique constraint on
    ``subscriptions.org_id`` would also reject it. This lets the same
    function back the ops CLI utility at
    ``backend/scripts/backfill_subscriptions.py`` without raising on
    re-runs.
    """
    plan_slug = _resolved_plan_slug()
    trial_days = _resolved_trial_days()
    today = datetime.date.today()
    trial_end = today + datetime.timedelta(days=trial_days)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    plan_row = bind.execute(_LOOKUP_PLAN, {"slug": plan_slug}).first()
    if plan_row is None:
        plan_row = bind.execute(_LOOKUP_FALLBACK_PLAN).first()
    if plan_row is None:
        # No active plan anywhere. Migration 023 seeds Free + Pro, so
        # this only fires on a DB that has been wiped post-023 or
        # before plans are seeded. Bail loudly so the deploy fails fast
        # rather than producing orphan rows.
        raise RuntimeError(
            "043_backfill_subscriptions: no active Plan row found. "
            "Migration 023 (plans_and_subscriptions) must have seeded "
            "Free + Pro; ensure plans.is_active rows exist before "
            "running this backfill."
        )
    plan_id = plan_row[0]

    org_ids = [
        row[0] for row in bind.execute(_ORGS_WITHOUT_SUBSCRIPTION).all()
    ]

    inserted = 0
    for org_id in org_ids:
        # Double-check inside the loop. The outer SELECT identified
        # orgs missing a subscription as of the query moment; the
        # per-row guard here makes the function safe to rerun (CLI
        # utility) and gives a portable idempotency signal that does
        # not depend on dialect-specific INSERT IGNORE / ON CONFLICT
        # syntax. The unique constraint on subscriptions.org_id is
        # still the hard guarantee.
        if bind.execute(_HAS_SUBSCRIPTION, {"org_id": org_id}).first() is not None:
            continue
        bind.execute(
            _INSERT_TRIAL,
            {
                "org_id": org_id,
                "plan_id": plan_id,
                "trial_start": today,
                "trial_end": trial_end,
                "now": now,
            },
        )
        inserted += 1

    return {
        "considered": len(org_ids),
        "inserted": inserted,
        "plan_slug": plan_slug,
        "plan_id": plan_id,
        "trial_days": trial_days,
    }


def upgrade() -> None:
    bind = op.get_bind()
    summary = backfill_missing_subscriptions(bind)
    # Print to stdout so the alembic subprocess log captures it; the
    # migrate wrapper (backend/scripts/migrate.py) streams alembic
    # output through unchanged, and downstream log shippers pick it up.
    print(
        f"migrate.backfill.subscriptions.summary {json.dumps(summary, default=str)}",
        file=sys.stdout,
        flush=True,
    )  # noqa: T201


def downgrade() -> None:
    """No-op. The up-migration is data-only and idempotent; reverting
    it would strand orgs without subscriptions. Pre-launch we hard-
    remove freely but the seeded trial rows are the floor going
    forward."""
    pass
