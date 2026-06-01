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
from app.models.report import Report, ReportVersion, ReportVisibility
from app.models.user import Role, User
from app.rate_limit import limiter
from app.reports.templates import get_report_templates
from app.schemas.report import (
    ReportCreate,
    ReportResponse,
    ReportTemplate,
    ReportUpdate,
    ReportVersionSummary,
)
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


# Retention: keep the original (never evicted) + this many most-recent
# non-original versions. Max 5 total per report.
MAX_NON_ORIGINAL_VERSIONS = 4


async def _snapshot_version(
    db: AsyncSession, report: Report, *, is_original: bool
) -> None:
    """Append a version row capturing the report's current live state.

    For non-original snapshots, enforces retention afterwards: the
    ``is_original`` row is never evicted; only the oldest non-original
    rows beyond ``MAX_NON_ORIGINAL_VERSIONS`` are deleted (ordered by
    ``created_at, id`` ascending). The caller is responsible for the
    surrounding ``commit``.
    """
    db.add(
        ReportVersion(
            report_id=report.id,
            is_original=is_original,
            layout_json=report.layout_json,
            canvas_filters_json=report.canvas_filters_json,
        )
    )
    if is_original:
        return

    await db.flush()
    stmt = (
        select(ReportVersion)
        .where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_original.is_(False),
        )
        .order_by(ReportVersion.created_at.asc(), ReportVersion.id.asc())
    )
    non_original = list((await db.execute(stmt)).scalars().all())
    excess = len(non_original) - MAX_NON_ORIGINAL_VERSIONS
    for stale in non_original[:max(excess, 0)]:
        await db.delete(stale)


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
    )
    db.add(row)
    await db.flush()
    await _snapshot_version(db, row, is_original=True)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/templates", response_model=list[ReportTemplate])
async def list_templates(current_user: User = Depends(get_current_user)):
    """Return the starter report templates (code fixtures).

    Registered ABOVE ``GET /{report_id}`` so the literal ``/templates``
    path is not captured by the ``{report_id}`` integer matcher.

    Calls ``get_report_templates()`` per request so the date windows are
    recomputed from ``date.today()`` and never go stale on a long-running
    backend.
    """
    return [ReportTemplate(**t) for t in get_report_templates()]


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
    versioned_keys = {"layout_json", "canvas_filters_json"}
    layout_changed = any(k in patch for k in versioned_keys)
    for k, v in patch.items():
        setattr(row, k, v)
    if layout_changed:
        # Capture the NEW live state as a non-original version (with
        # retention) only when the canvas itself changed. A rename /
        # visibility-only edit does not add to the history.
        await _snapshot_version(db, row, is_original=False)
    await db.commit()
    await db.refresh(row)
    return row


@router.get(
    "/{report_id}/versions",
    response_model=list[ReportVersionSummary],
)
async def list_report_versions(
    report_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List a report's version history, newest-first.

    Each entry is a lightweight summary (``id`` / ``is_original`` /
    ``created_at``); the full layout payload is not returned in the list.
    404 when the report is missing or invisible (mirrors GET).
    """
    row = await db.get(Report, report_id)
    if row is None or not _can_view(current_user, row):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    stmt = (
        select(ReportVersion)
        .where(ReportVersion.report_id == report_id)
        .order_by(ReportVersion.created_at.desc(), ReportVersion.id.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _restore_version(
    db: AsyncSession,
    report: Report,
    version: ReportVersion,
) -> Report:
    """Copy a version's layout/filters into the live report and commit.

    Restoring does NOT itself create a new version; the next Save does.
    """
    report.layout_json = version.layout_json
    report.canvas_filters_json = version.canvas_filters_json
    await db.commit()
    await db.refresh(report)
    return report


@router.post(
    "/{report_id}/versions/{version_id}/restore",
    response_model=ReportResponse,
)
async def restore_report_version(
    report_id: int,
    version_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore a specific version into the report's live state.

    404 when the report is missing/invisible, 403 when the caller lacks
    edit rights, 404 when the version does not belong to this report.
    Does NOT create a new version.
    """
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
    version = await db.get(ReportVersion, version_id)
    if version is None or version.report_id != report_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Version not found",
        )
    return await _restore_version(db, row, version)


@router.post("/{report_id}/reset", response_model=ReportResponse)
async def reset_report(
    report_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revert a report's live state to its as-created (original) version.

    Sugar over the version-restore path: finds this report's
    ``is_original=True`` version and restores it, so the existing
    "Revert to original" button keeps working.

    Visibility / edit gating mirrors PATCH + DELETE: 404 when the row is
    missing or invisible, 403 when the caller lacks edit rights.
    """
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
    stmt = (
        select(ReportVersion)
        .where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_original.is_(True),
        )
        .order_by(ReportVersion.id.asc())
    )
    original = (await db.execute(stmt)).scalars().first()
    if original is None:
        # No original version recorded (legacy / seeded row without
        # backfill). Nothing to restore; return the live state unchanged.
        return row
    return await _restore_version(db, row, original)


@router.post(
    "/{report_id}/duplicate",
    response_model=ReportResponse,
    status_code=201,
)
async def duplicate_report(
    report_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a private copy of a visible report, owned by the caller.

    404 when the source is missing or invisible (mirrors GET). The copy
    is always ``private`` regardless of the source's visibility, carries
    the source name with a " (copy)" suffix, and reuses the same
    creation path as ``create_report`` so it gets its own
    ``is_original`` version snapshot.
    """
    source = await db.get(Report, report_id)
    if source is None or not _can_view(current_user, source):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    row = Report(
        owner_user_id=current_user.id,
        org_id=current_user.org_id,
        visibility=ReportVisibility.PRIVATE,
        name=f"{source.name} (copy)",
        description=source.description,
        layout_json=source.layout_json,
        canvas_filters_json=source.canvas_filters_json,
    )
    db.add(row)
    await db.flush()
    await _snapshot_version(db, row, is_original=True)
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
