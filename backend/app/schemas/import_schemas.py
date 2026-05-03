"""Pydantic schemas for the transaction import flow (preview + confirm)."""

import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.transaction import DuplicateCandidate, TransferCandidate


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

    # Existing duplicate-detection (different from transfer-leg duplicate)
    is_duplicate: bool = False
    duplicate_transaction_id: int | None = None

    # Smart-rules suggestion
    suggested_category_id: int | None = None
    suggestion_source: Literal["org_rule", "shared_dictionary", "default"] | None = None

    # Detector 1: matches an already-linked leg on the same account → drop default
    is_duplicate_of_linked_leg: bool = False
    duplicate_candidate: DuplicateCandidate | None = None
    default_action_drop: bool = False

    # Detector 2: cross-account un-linked match (transfer-pair candidate)
    transfer_match_action: Literal["none", "pair_with", "suggest_pair", "choose_candidate"] = "none"
    transfer_match_confidence: Literal["same_day", "near_date", "multi_candidate"] | None = None
    pair_with_transaction_id: int | None = None
    transfer_candidates: list[TransferCandidate] = []

    model_config = ConfigDict(extra="forbid")


class ImportPreviewResponse(BaseModel):
    """Full preview result returned after parsing a CSV file."""

    rows: list[ImportPreviewRow]
    account_id: int
    file_name: str
    total_rows: int
    duplicate_count: int

    # New per-spec §3.2 summary counters
    auto_paired_count: int = 0
    suggested_pair_count: int = 0
    multi_candidate_count: int = 0
    duplicate_of_linked_count: int = 0


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

    # Spec §3.2 confirm-row action mapping
    action: Literal["create", "pair_with_existing", "drop_as_duplicate"] = "create"
    pair_with_transaction_id: int | None = None      # required iff action == "pair_with_existing"
    duplicate_of_transaction_id: int | None = None   # required iff action == "drop_as_duplicate"
    transfer_category_id: int | None = None
    recategorize: bool = True

    # Echoed back from preview for accept-vs-override detection
    suggested_category_id: int | None = None
    suggestion_source: Literal["org_rule", "shared_dictionary", "default"] | None = None

    model_config = ConfigDict(extra="forbid")


class ImportConfirmRequest(BaseModel):
    """Batch confirm request — the user submits all reviewed rows at once."""

    account_id: int
    default_category_id: int
    rows: list[ImportConfirmRow]

    model_config = ConfigDict(extra="forbid")


# ── Confirm Response ─────────────────────────────────────────────────────────


class ImportRowError(BaseModel):
    """Error detail for a single row that failed during import."""

    row_number: int
    error: str


class ImportConfirmResponse(BaseModel):
    """Result of the import execution.

    Counters sum to the total submitted rows:
      imported_count + paired_count + dropped_duplicate_count
        + skipped_count + error_count == total_rows.
    """

    imported_count: int          # plain rows created via action == "create"
    paired_count: int = 0        # rows confirmed action == "pair_with_existing"
    dropped_duplicate_count: int = 0   # rows confirmed action == "drop_as_duplicate"
    skipped_count: int           # rows with skip=True
    error_count: int
    errors: list[ImportRowError]
