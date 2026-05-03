import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    category_id: int
    description: str = Field(max_length=255)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    type: Literal["income", "expense"]
    status: Literal["settled", "pending"] = "settled"
    date: datetime.date

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Description is required")
        return v.strip()


class TransferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_account_id: int
    to_account_id: int
    category_id: Optional[int] = None
    description: str = Field(default="", max_length=255)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    status: Literal["settled", "pending"] = "settled"
    date: datetime.date


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: Optional[int] = None
    category_id: Optional[int] = None
    description: Optional[str] = None
    amount: Optional[Decimal] = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    type: Optional[Literal["income", "expense"]] = None
    status: Optional[Literal["settled", "pending"]] = None
    date: Optional[datetime.date] = None

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("Description is required")
        return v.strip() if v is not None else v


class TransactionResponse(BaseModel):
    id: int
    account_id: int
    account_name: str = ""
    category_id: int
    category_name: str = ""
    description: str
    amount: Decimal
    type: Literal["income", "expense"]
    status: Literal["settled", "pending"]
    linked_transaction_id: Optional[int] = None
    recurring_id: Optional[int] = None
    date: datetime.date
    settled_date: datetime.date | None = None
    is_imported: bool = False

    model_config = {"from_attributes": True}


class BulkDeleteRequest(BaseModel):
    """Body for POST /api/v1/transactions/bulk-delete."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Transaction IDs to delete. Cross-org IDs are silently ignored. Transfer-pair halves cascade.",
    )


class BulkDeleteResponse(BaseModel):
    """Result of a bulk delete."""

    requested_count: int
    deleted_count: int
    skipped_ids: list[int]  # IDs that were requested but not found in this org


class TransactionPairRequest(BaseModel):
    """Op-1 bulk-link / import-confirm-pair / Op-2 pair-with-existing payload."""

    model_config = ConfigDict(extra="forbid")

    expense_id: int
    income_id: int
    transfer_category_id: int | None = None
    recategorize: bool = True


class ConvertToTransferRequest(BaseModel):
    """Op-2 (pair existing) + Op-3 (create missing leg) per-row payload.

    If pair_with_transaction_id is set, the candidate's account_id MUST equal
    destination_account_id (server validates and raises ValidationError on
    mismatch). If unset, the service creates the partner leg on
    destination_account_id.
    """

    model_config = ConfigDict(extra="forbid")

    destination_account_id: int
    pair_with_transaction_id: int | None = None
    transfer_category_id: int | None = None
    recategorize: bool = True


class UnpairTransactionRequest(BaseModel):
    """Op-4 unlink payload. Per-leg fallback categories required because the
    Transfer system category no longer fits once the rows are unlinked.
    """

    model_config = ConfigDict(extra="forbid")

    expense_fallback_category_id: int
    income_fallback_category_id: int


class TransferCandidate(BaseModel):
    """A potential pair candidate returned by GET /transfer-candidates and
    embedded in import preview rows when transfer_match_action requires the
    user to choose.
    """

    id: int
    date: datetime.date
    description: str
    amount: Decimal
    account_id: int
    account_name: str
    date_diff_days: int
    confidence: Literal["same_day", "near_date"]


class TransferCandidatesResponse(BaseModel):
    """Wrapper for GET /api/v1/transactions/{id}/transfer-candidates."""

    candidates: list[TransferCandidate]


class DuplicateCandidate(BaseModel):
    """Embedded in ImportPreviewRow when a CSV row matches an existing linked
    leg on the same account. Lean shape so /import does not refetch row details.

    The synthetic-leg badge keys off existing_leg_is_imported (the matched leg
    itself, not its partner). False means the matched leg was created via Op-3
    convert-and-create or via the manual create-transfer UI.
    """

    id: int
    date: datetime.date
    description: str
    amount: Decimal
    account_id: int
    account_name: str
    existing_leg_is_imported: bool
