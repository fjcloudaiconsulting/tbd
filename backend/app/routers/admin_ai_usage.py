"""Superadmin debug endpoint for AI usage aggregation (PR2 of AI tier train).

Single endpoint:

- ``GET /api/v1/admin/ai/usage?org_id=...&period=YYYY-MM`` returns
  aggregated usage for the period. Useful when a soft-cap warning
  fires and ops wants to see which feature drove the spend before
  PR3 ships a customer-facing usage page.

Superadmin only — never reachable by a regular admin, even for
their own org. Cross-org reads are the *point* of this surface; a
regular admin's audit log already has enough breadcrumbs for their
own org.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.user import User


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin/ai", tags=["admin-ai"])


_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _parse_period(period: str) -> tuple[datetime, datetime]:
    """Return ``(start_inclusive, end_exclusive)`` for the ``YYYY-MM`` period.

    Naive datetimes — the ledger column is naive on both backends, so
    we match the comparison shape directly.
    """
    match = _PERIOD_RE.match(period)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_period_format"},
        )
    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_period_format"},
        )
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return (start, end)


async def require_superadmin(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    return current_user


@router.get("/usage")
async def get_usage(
    org_id: int = Query(..., ge=1),
    period: str = Query(..., description="YYYY-MM"),
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate AI usage for an org for the given period.

    Response shape::

        {
          "org_id": 42,
          "period": "2026-05",
          "total_prompt_tokens": 1234,
          "total_completion_tokens": 567,
          "total_cost_cents": 89,
          "by_feature": [
            {
              "feature_key": "chat",
              "prompt_tokens": ...,
              "completion_tokens": ...,
              "cost_cents": ...,
              "calls": 3,
              "failed_calls": 0
            },
            ...
          ]
        }

    Failed-call rows (success=false) are included in the call counts
    but their token/cost contributions are zero per spec.
    """
    start, end = _parse_period(period)

    totals_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(AIUsageLedger.prompt_tokens), 0),
                func.coalesce(func.sum(AIUsageLedger.completion_tokens), 0),
                func.coalesce(func.sum(AIUsageLedger.est_cost_cents), 0),
                func.count(),
            ).where(
                AIUsageLedger.org_id == org_id,
                AIUsageLedger.dispatched_at >= start,
                AIUsageLedger.dispatched_at < end,
            )
        )
    ).one()
    total_prompt, total_completion, total_cost, total_calls = totals_row

    by_feature_rows = (
        await db.execute(
            select(
                AIUsageLedger.feature_key,
                func.coalesce(func.sum(AIUsageLedger.prompt_tokens), 0),
                func.coalesce(func.sum(AIUsageLedger.completion_tokens), 0),
                func.coalesce(func.sum(AIUsageLedger.est_cost_cents), 0),
                func.count(),
                func.coalesce(
                    func.sum(
                        case(
                            (AIUsageLedger.success == False, 1),  # noqa: E712
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .where(
                AIUsageLedger.org_id == org_id,
                AIUsageLedger.dispatched_at >= start,
                AIUsageLedger.dispatched_at < end,
            )
            .group_by(AIUsageLedger.feature_key)
            .order_by(AIUsageLedger.feature_key)
        )
    ).all()

    by_feature = [
        {
            "feature_key": row[0],
            "prompt_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "cost_cents": int(row[3] or 0),
            "calls": int(row[4] or 0),
            "failed_calls": int(row[5] or 0),
        }
        for row in by_feature_rows
    ]

    return {
        "org_id": org_id,
        "period": period,
        "total_prompt_tokens": int(total_prompt or 0),
        "total_completion_tokens": int(total_completion or 0),
        "total_cost_cents": int(total_cost or 0),
        "total_calls": int(total_calls or 0),
        "by_feature": by_feature,
    }
