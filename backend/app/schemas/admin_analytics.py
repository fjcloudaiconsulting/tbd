"""Pydantic schemas for the L4.6 system-usage analytics API.

Counts-only first slice — no PII, no per-user series, no charts. The
envelope is intentionally flat so a follow-up PR can drop in a charts
client without re-shaping existing fields.
"""
from __future__ import annotations

import datetime
from typing import Optional

from pydantic import BaseModel


class DailyCount(BaseModel):
    """One bucket in a daily activity series. ``date`` is an ISO date
    (UTC). Missing days carry ``count=0`` so the frontend can render
    sparkline-style strips without re-walking the window."""

    date: datetime.date
    count: int


class OrgTxVolume(BaseModel):
    """Org ranked by transactions created in the window. ``rank`` is
    1-based to match the way humans read leaderboards."""

    rank: int
    org_id: int
    org_name: str
    tx_count: int


class DormantOrg(BaseModel):
    """Org with no transactions created within the dormancy window.

    ``last_tx_at`` is None when an org has never recorded a transaction
    (newly-created shells). ``days_since_last_activity`` is None in the
    same case — UI renders that as ``no activity yet`` rather than
    ``Infinity days``.
    """

    org_id: int
    org_name: str
    last_tx_at: Optional[datetime.datetime] = None
    days_since_last_activity: Optional[int] = None


class AnalyticsResponse(BaseModel):
    """One round-trip payload for ``/admin/analytics``.

    ``window_days`` is echoed so the frontend doesn't need to track the
    backend default separately. ``generated_at`` lets the UI render a
    "as of …" timestamp without trusting client clocks.
    """

    window_days: int
    generated_at: datetime.datetime
    logins_by_day: list[DailyCount]
    tx_writes_by_day: list[DailyCount]
    imports_by_day: list[DailyCount]
    top_orgs_by_tx_volume: list[OrgTxVolume]
    dormant_orgs: list[DormantOrg]
