"""Import service — orchestrates CSV preview and transaction creation.

Parsing/validation is separate from persistence so a background worker
can replace the synchronous confirm path later without a rewrite.
"""

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.schemas.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewRow,
    ImportPreviewResponse,
    ImportRowError,
)
from app.schemas.transaction import TransactionCreate, TransferCreate
from app.services import transaction_service
from app.services.import_parser import ParsedRow

logger = structlog.get_logger()

# Transaction types that suggest an inter-account transfer
_TRANSFER_TYPES = {"online banking"}


async def build_preview(
    db: AsyncSession,
    org_id: int,
    account_id: int,
    file_name: str,
    parsed_rows: list[ParsedRow],
) -> ImportPreviewResponse:
    """Build a preview response: flag duplicates and potential transfers."""

    preview_rows: list[ImportPreviewRow] = []
    duplicate_count = 0
    transfer_count = 0

    for row in parsed_rows:
        # ── Duplicate check: date + amount + description ──
        dup_result = await db.execute(
            select(Transaction.id).where(
                and_(
                    Transaction.org_id == org_id,
                    Transaction.account_id == account_id,
                    Transaction.date == row.date,
                    Transaction.amount == row.amount,
                    Transaction.description == row.description,
                )
            )
        )
        dup_id = dup_result.scalar_one_or_none()
        is_dup = dup_id is not None

        # ── Transfer detection: heuristic on transaction_type ──
        is_transfer = bool(
            row.transaction_type
            and row.transaction_type.lower() in _TRANSFER_TYPES
        )

        if is_dup:
            duplicate_count += 1
        if is_transfer:
            transfer_count += 1

        preview_rows.append(
            ImportPreviewRow(
                row_number=row.row_number,
                date=row.date,
                description=row.description,
                amount=row.amount,
                type=row.type,
                counterparty=row.counterparty,
                transaction_type=row.transaction_type,
                is_duplicate=is_dup,
                duplicate_transaction_id=dup_id,
                is_potential_transfer=is_transfer,
            )
        )

    return ImportPreviewResponse(
        rows=preview_rows,
        account_id=account_id,
        file_name=file_name,
        total_rows=len(preview_rows),
        duplicate_count=duplicate_count,
        transfer_candidate_count=transfer_count,
    )


async def execute_import(
    db: AsyncSession,
    org_id: int,
    body: ImportConfirmRequest,
) -> ImportConfirmResponse:
    """Create transactions for all confirmed (non-skipped) rows.

    Uses the existing create_transaction / create_transfer service functions
    so all validation, balance mutations, and locking are preserved.
    """
    imported_count = 0
    skipped_count = 0
    errors: list[ImportRowError] = []

    for row in body.rows:
        if row.skip:
            skipped_count += 1
            continue

        category_id = row.category_id or body.default_category_id

        try:
            if row.is_transfer and row.transfer_account_id:
                # Determine direction: the imported account is source for expenses,
                # destination for income.
                if row.type == "expense":
                    from_id = body.account_id
                    to_id = row.transfer_account_id
                else:
                    from_id = row.transfer_account_id
                    to_id = body.account_id

                transfer_body = TransferCreate(
                    from_account_id=from_id,
                    to_account_id=to_id,
                    description=row.description,
                    amount=row.amount,
                    date=row.date,
                )
                await transaction_service.create_transfer(
                    db, org_id, transfer_body, is_imported=True
                )
            else:
                tx_body = TransactionCreate(
                    account_id=body.account_id,
                    category_id=category_id,
                    description=row.description,
                    amount=row.amount,
                    type=row.type,
                    date=row.date,
                )
                await transaction_service.create_transaction(
                    db, org_id, tx_body, is_imported=True
                )

            imported_count += 1

        except Exception as exc:
            await logger.awarning(
                "import_row_failed",
                row_number=row.row_number,
                error=str(exc),
            )
            errors.append(ImportRowError(row_number=row.row_number, error=str(exc)))

    return ImportConfirmResponse(
        imported_count=imported_count,
        skipped_count=skipped_count,
        error_count=len(errors),
        errors=errors,
    )
