"""Import router -- CSV upload, preview, confirm, and reconciliation.

Wave 1 contract additions (2026-05-12, L3.2): OFX preview endpoint and
post-import reconciliation endpoint. L3.2 Wave 2B (this PR) replaces
the 501 reconciliation stub with a working state-machine handler. See
spec at ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.
"""

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.import_reconciliation import (
    ImportBatchDetail,
    ReconcileBatchRequest,
    ReconcileBatchResponse,
)
from app.schemas.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
)
from app.services import import_service, reconciliation_service
from app.services.exceptions import MissingCategoryTypeError, ValidationError
from app.services.import_abn_tab import parse_tab
from app.services.import_ofx_service import parse_ofx
from app.services.import_parser import ParseError, parse_csv

logger = structlog.get_logger()

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

router = APIRouter(prefix="/api/v1/import", tags=["import"])


def _missing_category_type_response(exc: MissingCategoryTypeError) -> HTTPException:
    """Translate a Layer B preflight failure into a structured 400.

    Frontend reads ``detail.code`` to render a targeted message (e.g.
    "you have no expense category"). ``message`` is the fallback copy
    when the client doesn't recognize the code.
    """
    return HTTPException(
        status_code=400,
        detail={
            "code": "missing_category_type",
            "missing_types": exc.missing_types,
            "message": exc.message,
        },
    )


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

    try:
        return await import_service.build_preview(
            db,
            org_id=current_user.org_id,
            account_id=account_id,
            file_name=file.filename or "unknown.csv",
            parsed_rows=parsed_rows,
            source_format="csv",
        )
    except MissingCategoryTypeError as exc:
        raise _missing_category_type_response(exc) from exc


@router.post("/tab/preview", response_model=ImportPreviewResponse)
async def preview_tab_import(
    file: UploadFile = File(...),
    account_id: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload an ABN AMRO ``.TAB`` file and get a preview.

    Mirrors the CSV ``/preview`` path: same 5 MB upload guard, same
    ``ParseError → ValidationError`` / ``MissingCategoryTypeError → 400``
    translation, same in-memory (no-persistence) preview. Decodes as
    UTF-8 and falls back to cp1252 (Dutch names/accents) on a decode
    error. Spec: ``specs/2026-06-09-abn-tab-import.md``.
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"File too large ({len(raw)} bytes, max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw.decode("cp1252")
        except UnicodeDecodeError:
            raise ValidationError(
                "File is not valid UTF-8 or Windows-1252 text"
            )

    try:
        parsed_rows = parse_tab(content)
    except ParseError as exc:
        detail = str(exc)
        if exc.row_number:
            detail = f"Row {exc.row_number}: {detail}"
        raise ValidationError(detail)

    try:
        return await import_service.build_preview(
            db,
            org_id=current_user.org_id,
            account_id=account_id,
            file_name=file.filename or "unknown.tab",
            parsed_rows=parsed_rows,
            source_format="tab",
        )
    except MissingCategoryTypeError as exc:
        raise _missing_category_type_response(exc) from exc


@router.post("/confirm", response_model=ImportConfirmResponse)
async def confirm_import(
    body: ImportConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute the import -- create transactions for all confirmed rows.

    L3.2 Wave 2B: passes ``user_id`` so the service can stamp the
    ``import_batches.created_by_user_id`` column when grouping the
    imported rows under a fresh batch header.
    """
    return await import_service.execute_import(
        db,
        org_id=current_user.org_id,
        body=body,
        user_id=current_user.id,
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
    try:
        return await import_service.build_preview(
            db,
            org_id=current_user.org_id,
            account_id=account_id,
            file_name=file.filename or "unknown.ofx",
            parsed_rows=parsed_rows,
            source_format="ofx",
        )
    except MissingCategoryTypeError as exc:
        raise _missing_category_type_response(exc) from exc


@router.get(
    "/{import_id}",
    response_model=ImportBatchDetail,
)
async def get_import_batch(
    import_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the header + per-row state of an import batch.

    Powers the reconciliation inbox UI. Per-row payload includes the
    current ``reconciliation_state`` and a ``duplicate_warning`` flag
    (set when ``fitid`` matches a transaction outside this batch).
    """
    return await reconciliation_service.get_batch_detail(
        db,
        org_id=current_user.org_id,
        batch_id=import_id,
    )


@router.post(
    "/{import_id}/reconcile",
    response_model=ReconcileBatchResponse,
)
async def reconcile_import_batch(
    import_id: int,
    body: ReconcileBatchRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply state-machine transitions to rows in an import batch.

    Contract: ``specs/2026-05-12-l3-2-import-contracts.md`` §3. All
    transitions in a request commit atomically (one savepoint). The
    batch auto-closes when ``remaining_pending`` hits 0.

    Disallowed transitions return 409 with the source + target state in
    the detail (``ConflictError`` -> global handler). Bad payload
    (missing edits / match target, transaction belongs to a different
    batch) returns 422. Missing batch returns 404.
    """
    response = await reconciliation_service.reconcile_request(
        db,
        org_id=current_user.org_id,
        batch_id=import_id,
        request=body,
    )
    await logger.ainfo(
        "import.reconcile.applied",
        org_id=current_user.org_id,
        batch_id=import_id,
        actor_user_id=current_user.id,
        ip=get_client_ip(request),
        transitioned=len(response.transitioned),
        remaining_pending=response.remaining_pending,
        batch_status=response.batch_status,
    )
    return response
