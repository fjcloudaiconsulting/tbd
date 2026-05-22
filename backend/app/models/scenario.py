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
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
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
