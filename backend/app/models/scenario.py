"""Plans simulation sandbox model (spec 2026-05-22).

Internal name = ``scenarios``; user-facing label = "Plans" (architect
lock 2026-05-22). The DB table, SQLAlchemy class, schemas, and router
prefix all use ``scenarios``. The UI never says "scenario".

Sandboxing contract: this model is the ONLY thing the simulation
engine writes to. It must not introduce any relationship that would
let the engine cascade a write into ``accounts``, ``transactions``,
``budgets``, ``recurring_transactions``, or ``forecast_plans``. The
guard test in ``tests/services/test_scenario_engine.py`` pins this
invariant with a row-count delta assertion across all five tables.

``params_json`` is a JSON blob validated by a Pydantic discriminated
union on ``scenario_type``. ``projection_json`` caches the last
``simulate`` output so the list view can render without re-running
the engine.

``horizon_months`` allows 1-480 at the column. The per-type cap
(120 / 480) is enforced at the request validator, not the column.

Plans redesign (TBD-548, schema ticket TBD-598): a scenario is now a plan
with a lifecycle (``status``) and goals in ``plan_goals``. The table is not
named ``plans``; subscription tiers own that name. The new FKs below are
columns only, all ``ON DELETE SET NULL``, so the sandboxing contract holds:
no relationship lets the engine write into the referenced tables. The legacy
columns stay until the cleanup ticket.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScenarioType(str, enum.Enum):
    TRIP = "trip"
    PURCHASE = "purchase"
    RETIREMENT = "retirement"
    CUSTOM = "custom"


class ScenarioStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    FINISHED = "finished"


class PlanGoalTier(str, enum.Enum):
    ESSENTIAL = "essential"
    WANT = "want"
    DREAM = "dream"


class PlanGoalKind(str, enum.Enum):
    LUMP = "lump"
    MONTHLY_SPEND = "monthly_spend"
    SAVINGS_TARGET = "savings_target"
    RETIREMENT = "retirement"


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    scenario_type: Mapped[ScenarioType] = mapped_column(
        Enum(
            ScenarioType,
            name="scenario_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    projection_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    projection_engine: Mapped[Optional[str]] = mapped_column(
        String(40), nullable=True
    )
    projection_computed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    horizon_months: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default="24"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    status: Mapped[ScenarioStatus] = mapped_column(
        Enum(
            ScenarioStatus,
            name="scenario_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=ScenarioStatus.DRAFT,
        server_default=ScenarioStatus.DRAFT.value,
    )
    # NULL means the org's primary currency (no backfill, TBD-598).
    currency: Mapped[Optional[str]] = mapped_column(CHAR(3), nullable=True)
    # Spend cap only; goals carry their own amounts.
    monthly_budget: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    birth_year: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    return_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0"), server_default="0.00"
    )
    inflation_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0"), server_default="0.00"
    )
    funding_account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # A master category; goals point at its children.
    category_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    tag_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Frozen at Start; ``projection_json`` keeps moving.
    baseline_projection_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_scenarios_org_user", "org_id", "user_id"),
        Index("ix_scenarios_org_active", "org_id", "is_active"),
    )


class PlanGoal(Base):
    """One goal inside a plan (a ``scenarios`` row). Deleted with its plan."""

    __tablename__ = "plan_goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    scenario_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    tier: Mapped[PlanGoalTier] = mapped_column(
        Enum(
            PlanGoalTier,
            name="plan_goal_tier",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    kind: Mapped[PlanGoalKind] = mapped_column(
        Enum(
            PlanGoalKind,
            name="plan_goal_kind",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    target_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    target_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    monthly_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # A child of the plan's master category.
    category_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Savings and retirement goals.
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    start_balance: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    funding_recurring_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("recurring_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    params_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_plan_goals_org_id", "org_id"),
        Index("ix_plan_goals_scenario_sort", "scenario_id", "sort_order"),
    )
