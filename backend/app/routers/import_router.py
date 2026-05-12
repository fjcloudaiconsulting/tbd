"""Import router — CSV upload, preview, and confirm endpoints.

Wave 1 contract additions (2026-05-12, L3.2): OFX preview endpoint and
post-import reconciliation endpoint. Both stubbed at 501 — Wave 2 teams
implement against the frozen schemas. See spec at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.import_reconciliation import (
    ReconcileBatchRequest,
    ReconcileBatchResponse,
)
from app.schemas.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
)
from app.services import import_service
from app.services.exceptions import ValidationError
from app.services.import_ofx_service import parse_ofx
from app.services.import_parser import ParseError, parse_csv

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

router = APIRouter(prefix="/api/v1/import", tags=["import"])


@router.post("/preview", response_model=ImportPreviewResponse)
async def preview_import(
    file: UploadFile = File(...),
    account_id: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a CSV file and get a preview with duplicate/transfer flags.

    The file is parsed in memory — no persistence happens at this stage.
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"File too large ({len(raw)} bytes, max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    content = raw.decode("utf-8-sig")  # handles BOM

    try:
        parsed_rows = parse_csv(content)
    except ParseError as exc:
        detail = str(exc)
        if exc.row_number:
            detail = f"Row {exc.row_number}: {detail}"
        raise ValidationError(detail)

    return await import_service.build_preview(
        db,
        org_id=current_user.org_id,
        account_id=account_id,
        file_name=file.filename or "unknown.csv",
        parsed_rows=parsed_rows,
    )


@router.post("/confirm", response_model=ImportConfirmResponse)
async def confirm_import(
    body: ImportConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute the import — create transactions for all confirmed rows."""
    return await import_service.execute_import(
        db,
        org_id=current_user.org_id,
        body=body,
    )


# ── L3.2 Wave 1 contract stubs ───────────────────────────────────────────────
# These endpoints exist to publish the OpenAPI shape so downstream Wave 2
# teams (OFX Parser, Reconciliation UI) can build against frozen contracts.
# Each stub returns 501 with a pointer to the L3.2 dispatch. Auth and
# org-scoping are wired so contract tests can assert them.


@router.post("/ofx/preview", response_model=ImportPreviewResponse)
async def preview_ofx_import(
    file: UploadFile = File(...),
    account_id: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload an OFX 1.x SGML / 2.x XML file and get a preview.

    Same response shape as the CSV preview; OFX-only extras
    (``fitid``, ``bank_id``, ``account_type_ofx``) populate the
    nullable fields on each row. Contract spec at
    ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    §1. Bounds enforced in ``app.services.import_ofx_service``.
    """
    raw = await file.read()
    try:
        parsed_rows = await parse_ofx(raw)
    except ParseError as exc:
        raise ValidationError(str(exc))
    return await import_service.build_preview(
        db,
        org_id=current_user.org_id,
        account_id=account_id,
        file_name=file.filename or "unknown.ofx",
        parsed_rows=parsed_rows,
    )


@router.post(
    "/{import_id}/reconcile",
    response_model=ReconcileBatchResponse,
    status_code=501,
)
async def reconcile_import_batch(
    import_id: int,
    body: ReconcileBatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """**STUB** — Post-import reconciliation endpoint, Wave 2 deliverable.

    Contract: applies state transitions to imported rows. All transitions
    in a request commit atomically (one savepoint). The batch
    auto-closes when ``remaining_pending`` hits 0.

    Frozen contract: see spec at
    ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    §3 (Reconciliation State Machine).

    Wave 2 Reconciliation UI team owns:
        - Migration: ``transactions.reconciliation_state`` enum column,
          ``transactions.import_batch_id`` FK, new ``import_batches`` table.
        - Service: state-transition validation, atomic apply, batch-close
          side-effect.
        - Frontend: inbox UX, per-row transition controls.
    """
    _ = (current_user.org_id, db, import_id, len(body.transitions))
    raise HTTPException(
        status_code=501,
        detail=(
            "Reconciliation not implemented — see L3.2 dispatch "
            "(specs/2026-05-12-l3-2-import-contracts.md §3)"
        ),
    )
