"""Import router — CSV upload, preview, and confirm endpoints."""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
)
from app.services import import_service
from app.services.import_parser import ParseError, parse_csv

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
    content = (await file.read()).decode("utf-8-sig")  # handles BOM

    try:
        parsed_rows = parse_csv(content)
    except ParseError as exc:
        from app.services.exceptions import ValidationError

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
