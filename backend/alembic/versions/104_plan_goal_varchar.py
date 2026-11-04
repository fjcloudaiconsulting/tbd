"""plan_goals.tier and plan_goals.kind to VARCHAR(32) (TBD-601).

Revision ID: 104_plan_goal_varchar
Revises: 099_recurring_transfers
Create Date: 2026-11-04

Chained on 099 on this branch; the lead re-points ``down_revision`` to
``103_recurring_origin`` at merge.

Migration 098 created both columns as native ENUMs. Goal kinds are expected
to grow, and with an ENUM every new value needs DDL and a pod still on the
previous release raises ``LookupError`` reading a row with a value it does
not know (the reason 096 moved ``org_ai_credentials.provider``). The columns
become VARCHAR(32), validated by the app (``PlanGoal._check_values``).

Live-safe: no goal has been written in production yet, so MySQL's table copy
for the type change is of an empty or near-empty table. Every value the
ENUMs hold fits VARCHAR(32), so no data changes. Rolling deploy: the previous
release reads the column through its ENUM type, which accepts every value
this release writes.

``downgrade`` restores the ENUMs exactly as 098 left them. It refuses,
without changing anything, when a row holds a value outside them: MySQL
would otherwise truncate it or fail half way.

Migrations never import ``app``: the value literals are below.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "104_plan_goal_varchar"
down_revision: Union[str, None] = "099_recurring_transfers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The ENUMs as of 098_plans_schema.
_COLUMNS = {
    "tier": sa.Enum("essential", "want", "dream", name="plan_goal_tier"),
    "kind": sa.Enum(
        "lump", "monthly_spend", "savings_target", "retirement", name="plan_goal_kind"
    ),
}
_VARCHAR = sa.String(length=32)


def upgrade() -> None:
    for name, enum in _COLUMNS.items():
        op.alter_column("plan_goals", name, existing_type=enum, type_=_VARCHAR, existing_nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    for name, enum in _COLUMNS.items():
        outside = bind.execute(
            sa.text(
                f"SELECT DISTINCT {name} FROM plan_goals WHERE {name} NOT IN :values"
            ).bindparams(sa.bindparam("values", expanding=True)),
            {"values": list(enum.enums)},
        ).scalars().all()
        if outside:
            raise RuntimeError(
                f"plan_goals.{name} holds values the old ENUM cannot store: "
                f"{sorted(outside)!r}. Delete or migrate those rows first."
            )
    for name, enum in _COLUMNS.items():
        op.alter_column("plan_goals", name, existing_type=_VARCHAR, type_=enum, existing_nullable=False)
