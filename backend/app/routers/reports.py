"""Reports v2 — substrate router (PR1 of the train).

Mounted at ``/api/v1/reports``. Every route is gated by
``require_reports_v2_enabled``, which raises ``HTTPException(404)`` when
``settings.feature_reports_v2`` is False. The architect-locked decision
(spec §11 "PR 1" + "Decisions locked" #1): the backend route-disable is
the load-bearing flag-off behaviour; the frontend additionally hides
the nav and routes.

Endpoints:

- ``POST /api/v1/reports/query`` — execute an AST against org data.
- ``GET    /api/v1/reports``      — list visible reports (own + org).
- ``POST   /api/v1/reports``      — create.
- ``GET    /api/v1/reports/{id}`` — fetch.
- ``PATCH  /api/v1/reports/{id}`` — update.
- ``DELETE /api/v1/reports/{id}`` — delete.

Permissions (spec §8):

- Visibility: owner always; org members when ``visibility=org`` and
  the org matches.
- Edit / delete: owner; org owner / admin when the report is
  org-shared.

System-level user-delete reassignment (org-shared -> org owner,
private -> hard delete) is a follow-up PR. PR1 only ensures the FK is
``ON DELETE RESTRICT`` so a code path that bypasses the service layer
fails loud instead of silently dropping reports.
"""
from typing import Optional

import structlog
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user
from app.models.report import Report, ReportVisibility
from app.models.user import Role, User
from app.rate_limit import limiter
from app.schemas.report import ReportCreate, ReportResponse, ReportUpdate
from app.schemas.reports_query import ReportsQuery, ReportsQueryResponse
from app.services.reports_query_service import execute_query


logger = structlog.stdlib.get_logger()


async def require_reports_v2_enabled() -> None:
    """Router-level dependency: hard 404 when the flag is off.

    The architect-locked gate (spec §11 "PR 1"):

    > When the flag is off, ALL ``/api/v1/reports/*`` routes return 404
    > via a router-level dependency that raises ``HTTPException(404)``.

    The 404 is the same shape FastAPI emits for an unknown path so the
    surface looks identical to a route that doesn't exist.
    """
    if not app_settings.feature_reports_v2:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )


router = APIRouter(
    prefix="/api/v1/reports",
    tags=["reports"],
    dependencies=[Depends(require_reports_v2_enabled)],
)


def _can_view(user: User, report: Report) -> bool:
    if report.owner_user_id == user.id:
        return True
    return (
        report.visibility == ReportVisibility.ORG
        and report.org_id == user.org_id
    )


def _can_edit(user: User, report: Report) -> bool:
    if report.owner_user_id == user.id:
        return True
    # Org owner / admin can edit org-shared reports inside their org.
    if (
        report.org_id == user.org_id
        and report.visibility == ReportVisibility.ORG
        and user.role in (Role.OWNER, Role.ADMIN)
    ):
        return True
    return False


@router.post(
    "/query",
    response_model=ReportsQueryResponse,
)
@limiter.limit("60/minute")
async def run_query(
    request: Request,
    body: ReportsQuery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a validated AST against the caller's org data.

    ``org_id`` is injected from ``current_user``; the AST has no way to
    express it. See ``app/services/reports_query_service.py``.
    """
    rows, meta = await execute_query(db, body, org_id=current_user.org_id)
    return ReportsQueryResponse(rows=rows, meta=meta)


@router.get("", response_model=list[ReportResponse])
async def list_reports(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List reports visible to this user.

    Visible = owner OR (org-visibility AND same org). Ordered by
    ``updated_at`` desc to match the list-page UX in the spec.
    """
    stmt = (
        select(Report)
        .where(
            (Report.owner_user_id == current_user.id)
            | (
                (Report.org_id == current_user.org_id)
                & (Report.visibility == ReportVisibility.ORG)
            )
        )
        .order_by(Report.updated_at.desc(), Report.id.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("", response_model=ReportResponse, status_code=201)
async def create_report(
    body: ReportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = Report(
        owner_user_id=current_user.id,
        org_id=current_user.org_id,
        visibility=body.visibility,
        name=body.name,
        description=body.description,
        layout_json=body.layout_json,
        canvas_filters_json=body.canvas_filters_json,
        original_layout_json=body.layout_json,
        original_canvas_filters_json=body.canvas_filters_json,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Report, report_id)
    if row is None or not _can_view(current_user, row):
        # Same 404 whether the row is missing or invisible — never
        # leak the existence of another org's report.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    return row


@router.patch("/{report_id}", response_model=ReportResponse)
async def update_report(
    report_id: int,
    body: ReportUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Report, report_id)
    if row is None or not _can_view(current_user, row):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    if not _can_edit(current_user, row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    patch = body.model_dump(exclude_unset=True)
    for k, v in patch.items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete(
    "/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_report(
    report_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Report, report_id)
    if row is None or not _can_view(current_user, row):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    if not _can_edit(current_user, row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
