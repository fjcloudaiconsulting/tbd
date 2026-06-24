"""Dashboard layout router — GET/PATCH /api/v1/dashboard.

Per-user customisable dashboard layout, gated behind
``Feature.CUSTOM_DASHBOARD`` (default OFF in env-floor; operator enables
per org via ``/system/features`` or the org Feature Access card).

Design decisions:

- **One row per user** — the ``UNIQUE(owner_user_id)`` DB constraint is
  the canonical enforcement. The GET endpoint auto-creates the row on
  first access using a SELECT-then-INSERT pattern. The UNIQUE constraint
  prevents duplicate rows if two concurrent first-access requests race;
  the losing INSERT raises an IntegrityError that surfaces as a 500 in
  that pathological case. There is no ``on_conflict_do_nothing`` upsert —
  handling that concurrent-first-access race is a known Phase-2
  follow-up.

- **org_id / owner_user_id from current_user only** — never taken from
  the wire. Mirrors the reports router's org-scoping convention.

- **DEFAULT_DASHBOARD_LAYOUT** — a minimal valid ``LayoutJson`` with a
  single KPI widget. Phase 2 will populate it with real finance tiles;
  Phase 1 just needs it to be a structurally-valid, non-empty layout so
  the frontend canvas has something to render on first boot.

- **validate-and-return-verbatim** — the PATCH handler stores the exact
  dict the caller sent (after Pydantic validation). No ``model_dump`` /
  reconstruction round-trip that might strip unmodelled visual knobs like
  ``compare_prior_period``, ``top_n``, ``smooth`` etc. (mirrors the #424
  fix in the reports router).
"""
from __future__ import annotations

import copy
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.dashboard import DashboardLayout
from app.models.user import User
from app.schemas.dashboard import DashboardLayoutOut, DashboardUpdate
from app.services.feature_gate import Feature, require_feature


logger = structlog.stdlib.get_logger()

# Minimal valid LayoutJson for Phase 1. Phase 2 will replace this with a
# curated set of finance tiles once the widget catalogue is richer.
DEFAULT_DASHBOARD_LAYOUT: dict = {
    "version": 1,
    "widgets": [
        {
            "id": "default-kpi-1",
            "type": "kpi",
            "title": "Total Spent",
            "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
            "config": {
                "dataset": "transactions",
                "measure": {"agg": "sum", "field": "amount"},
            },
        }
    ],
}

DEFAULT_CANVAS_FILTERS: dict = {}


router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_feature(Feature.CUSTOM_DASHBOARD))],
)


async def _get_or_create(db: AsyncSession, user: User) -> DashboardLayout:
    """Return the caller's DashboardLayout row, auto-creating on first access.

    Uses SELECT-first to avoid a superfluous INSERT on every GET. The UNIQUE
    constraint on ``owner_user_id`` is the race-safety net: a concurrent
    first-access pair would have one INSERT succeed and one raise
    IntegrityError (surfaced as a 500 only in that pathological case, which
    the P2 upsert can address if needed).
    """
    stmt = select(DashboardLayout).where(
        DashboardLayout.owner_user_id == user.id
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is not None:
        return row

    row = DashboardLayout(
        owner_user_id=user.id,
        org_id=user.org_id,
        layout_json=copy.deepcopy(DEFAULT_DASHBOARD_LAYOUT),
        canvas_filters_json=copy.deepcopy(DEFAULT_CANVAS_FILTERS),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("", response_model=DashboardLayoutOut)
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the caller's dashboard layout, auto-creating the default on
    first access.

    ``org_id`` / ``owner_user_id`` are taken from ``current_user`` only —
    never from the wire.
    """
    return await _get_or_create(db, current_user)


@router.patch("", response_model=DashboardLayoutOut)
async def update_dashboard(
    body: DashboardUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the caller's dashboard layout.

    Auto-creates the row on first access (so the caller can PATCH without
    a prior GET), then applies the patch. Only
    ``layout_json`` and ``canvas_filters_json`` are patchable; both are
    optional in a single call.

    Explicit null for either column is rejected with 422 (NOT-NULL columns).

    The validated dict is stored and returned VERBATIM — no model_dump /
    reconstruction that would silently strip unmodelled visual knobs
    (``compare_prior_period``, ``top_n``, ``smooth``, etc.).
    """
    row = await _get_or_create(db, current_user)

    patch = body.model_dump(exclude_unset=True)
    nullable_columns = {"layout_json", "canvas_filters_json"}
    for k in nullable_columns:
        if k in patch and patch[k] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{k} may not be null",
            )

    for k, v in patch.items():
        setattr(row, k, v)

    await db.commit()
    await db.refresh(row)
    return row
