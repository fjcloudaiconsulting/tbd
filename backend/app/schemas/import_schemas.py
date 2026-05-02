"""Pydantic schemas for the transaction import flow (preview + confirm)."""

import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


# ── Preview Response ─────────────────────────────────────────────────────────


class ImportPreviewRow(BaseModel):
    """A single parsed row returned by the preview endpoint."""

    row_number: int
    date: datetime.date
    description: str
    amount: Decimal
    type: Literal["income", "expense"]
    counterparty: str | None = None
    transaction_type: str | None = None
    is_duplicate: bool = False
    duplicate_transaction_id: int | None = None
    is_potential_transfer: bool = False
    suggested_category_id: int | None = None
    suggestion_source: Literal["org_rule", "shared_dictionary", "default"] | None = None


class ImportPreviewResponse(BaseModel):
    """Full preview result returned after parsing a CSV file."""

    rows: list[ImportPreviewRow]
    account_id: int
    file_name: str
    total_rows: int
    duplicate_count: int
    transfer_candidate_count: int


# ── Confirm Request ──────────────────────────────────────────────────────────


class ImportConfirmRow(BaseModel):
    """A single row in the confirm request — user has reviewed and annotated."""

    row_number: int
    date: datetime.date
    description: str
    amount: Decimal = Field(gt=0)
    type: Literal["income", "expense"]
    category_id: int | None = None  # None → use default_category_id
    skip: bool = False
    is_transfer: bool = False
    transfer_account_id: int | None = None  # required when is_transfer=True
    suggested_category_id: int | None = None  # echoed back from preview for accept-vs-override detection
    suggestion_source: Literal["org_rule", "shared_dictionary", "default"] | None = None


class ImportConfirmRequest(BaseModel):
    """Batch confirm request — the user submits all reviewed rows at once."""

    account_id: int
    default_category_id: int
    rows: list[ImportConfirmRow]


# ── Confirm Response ─────────────────────────────────────────────────────────


class ImportRowError(BaseModel):
    """Error detail for a single row that failed during import."""

    row_number: int
    error: str


class ImportConfirmResponse(BaseModel):
    """Result of the import execution."""

    imported_count: int
    skipped_count: int
    error_count: int
    errors: list[ImportRowError]
