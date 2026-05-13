"""Pydantic schemas for the L4.5 subscription & revenue admin surface.

Counts and identity only — every dollar figure is mocked since real
payments (L2.2) are parked. The ``mock_revenue`` flag in the KPI
envelope is the single source of truth the frontend reads when
deciding whether to render ``($0 — payments not live)`` next to dollar
values. Once L2 wires real billing, flip the flag and the UI starts
showing real money without a schema change.
"""
from __future__ import annotations

import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel


SubscriptionStatusLiteral = Literal[
    "trialing", "active", "past_due", "canceled"
]


class SubscriptionListItem(BaseModel):
    """One row in the cross-org subscriptions table.

    ``org_name`` is the snapshot value at read time. We always join via
    ``Subscription.org_id`` so a renamed org reflects in the next
    refresh — there is no audit-style snapshot pinning here because the
    surface is for live triage, not historical attestation.
    """

    subscription_id: int
    org_id: int
    org_name: str
    plan_id: Optional[int] = None
    plan_slug: Optional[str] = None
    plan_name: Optional[str] = None
    status: SubscriptionStatusLiteral
    billing_interval: Literal["monthly", "yearly"]
    trial_start: Optional[datetime.date] = None
    trial_end: Optional[datetime.date] = None
    current_period_start: Optional[datetime.date] = None
    current_period_end: Optional[datetime.date] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class SubscriptionListResponse(BaseModel):
    items: list[SubscriptionListItem]
    total: int
    limit: int
    offset: int


class PlanInfo(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    price_monthly: str  # Decimal serialized as string per FE convention.
    price_yearly: str
    max_users: Optional[int] = None
    retention_days: Optional[int] = None
    features: dict[str, Any] = {}
    is_custom: bool
    is_active: bool


class OrgInfo(BaseModel):
    id: int
    name: str
    billing_cycle_day: int
    created_at: Optional[datetime.datetime] = None
    member_count: int


class FeatureOverrideSnapshot(BaseModel):
    """A single active override on the subscription's org.

    Source: ``org_feature_overrides`` (L4.11). The detail page shows
    these read-only — grant/revoke still lives on ``/admin/orgs/[id]``
    so L4.11 stays the single source of truth for the mutation flow.
    """

    feature_key: str
    value: bool
    set_at: Optional[datetime.datetime] = None
    expires_at: Optional[datetime.datetime] = None
    is_expired: bool
    note: Optional[str] = None


class SubscriptionDetail(BaseModel):
    subscription_id: int
    org: OrgInfo
    plan: Optional[PlanInfo] = None
    status: SubscriptionStatusLiteral
    billing_interval: Literal["monthly", "yearly"]
    trial_start: Optional[datetime.date] = None
    trial_end: Optional[datetime.date] = None
    current_period_start: Optional[datetime.date] = None
    current_period_end: Optional[datetime.date] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    feature_overrides: list[FeatureOverrideSnapshot]
    # Mock revenue dollar amount. Always 0 today; the field exists so
    # the FE never has to special-case its absence after L2 lands.
    mock_revenue_amount: str = "0.00"
    mock_revenue: bool = True


class SubscriptionKPIs(BaseModel):
    """Revenue / lifecycle pulse strip for the list page header.

    ``mock_revenue`` is always ``True`` until L2.2 ships. The FE reads
    the flag and renders dollar columns with a ``mock`` badge plus a
    tooltip explaining payments aren't live yet.
    """

    total_subscriptions: int
    active: int
    trial: int
    past_due: int
    cancelled: int
    signups_last_7d: int
    trial_expiring_next_7d: int
    plan_distribution: list["PlanDistributionItem"]
    # Mock dollar figures — see module docstring. The FE renders these
    # with explicit "mock" labelling so admins never confuse the
    # surface for real revenue. Keep them as strings so adding a
    # currency code later (LAI / L2.2) doesn't widen the type.
    mock_mrr: str = "0.00"
    mock_arr: str = "0.00"
    mock_revenue: bool = True
    generated_at: datetime.datetime


class PlanDistributionItem(BaseModel):
    plan_id: Optional[int] = None
    plan_slug: Optional[str] = None
    plan_name: Optional[str] = None
    subscription_count: int


# Resolves forward reference in SubscriptionKPIs.
SubscriptionKPIs.model_rebuild()
