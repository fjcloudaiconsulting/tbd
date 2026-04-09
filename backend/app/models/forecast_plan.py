import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class PlanStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"


class ForecastItemType(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"


class ItemSource(str, enum.Enum):
    MANUAL = "manual"
    RECURRING = "recurring"
    HISTORY = "history"


class ForecastPlan(Base):
    __tablename__ = "forecast_plans"
    __table_args__ = (
        UniqueConstraint("org_id", "billing_period_id", name="uq_forecast_plan_org_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False)
    billing_period_id: Mapped[int] = mapped_column(Integer, ForeignKey("billing_periods.id"), nullable=False)
    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        server_default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    billing_period = relationship("BillingPeriod", lazy="selectin")
    items: Mapped[list["ForecastPlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", lazy="selectin",
    )


class ForecastPlanItem(Base):
    __tablename__ = "forecast_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "category_id", "type", name="uq_forecast_item_plan_cat_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("forecast_plans.id", ondelete="CASCADE"), nullable=False)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    type: Mapped[ForecastItemType] = mapped_column(
        Enum(ForecastItemType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    planned_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    source: Mapped[ItemSource] = mapped_column(
        Enum(ItemSource, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        server_default="manual",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    plan: Mapped["ForecastPlan"] = relationship(back_populates="items")
    category = relationship("Category", lazy="raise")
