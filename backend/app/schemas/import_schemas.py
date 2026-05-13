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

    # L3.2 Wave 1 contract: OFX-specific extras (populated only by the OFX
    # preview path; NULL on the CSV path). Declared here so OpenAPI exposes
    # the wire shape Wave 2 teams build against. See spec
    # ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    # §1 for field semantics; ``fitid`` is the primary OFX dedup signal
    # (per OFX spec §11.4.4, unique within bank+account).
    fitid: str | None = None
    bank_id: str | None = None
    account_type_ofx: Literal["CHECKING", "SAVINGS", "CREDITLINE", "MONEYMRKT"] | None = None

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

    # L3.2 Wave 2B: source format of the parsed file ('csv' or 'ofx').
    # The frontend echoes this back at confirm so the service can stamp
    # the new ``import_batches`` row with the correct origin.
    source_format: str | None = None


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
    action: Literal[
        "create", "pair_with_existing", "drop_as_duplicate", "create_transfer_pair"
    ] = "create"
    pair_with_transaction_id: int | None = None      # required iff action == "pair_with_existing"
    duplicate_of_transaction_id: int | None = None   # required iff action == "drop_as_duplicate"
    partner_account_id: int | None = None            # required iff action == "create_transfer_pair"
    transfer_category_id: int | None = None
    recategorize: bool = True

    # Echoed back from preview for accept-vs-override detection
    suggested_category_id: int | None = None
    suggestion_source: Literal["org_rule", "shared_dictionary", "default"] | None = None

    # L3.2 Wave 1 contract: OFX-specific extras echoed from the preview row
    # so the confirm payload can carry them through to audit / future
    # locale dispatch. Always NULL on the CSV path.
    fitid: str | None = None
    bank_id: str | None = None
    account_type_ofx: Literal["CHECKING", "SAVINGS", "CREDITLINE", "MONEYMRKT"] | None = None

    model_config = ConfigDict(extra="forbid")


class ImportConfirmRequest(BaseModel):
    """Batch confirm request -- the user submits all reviewed rows at once.

    L3.2 Wave 2B: ``file_name`` and ``source_format`` are optional metadata
    that drive ``import_batches`` header creation. When provided, the
    confirm path groups the resulting transactions under a fresh
    ``ImportBatch`` so the reconciliation inbox can list them. The
    fields default to ``None`` to preserve backward compatibility with
    the pre-reconciliation client, which never sent them.
    """

    account_id: int
    default_category_id: int
    rows: list[ImportConfirmRow]
    # Optional metadata for the new ``import_batches`` header. The frontend
    # echoes ``file_name`` and ``source_format`` from the preview response;
    # the service skips batch creation if either is omitted.
    file_name: str | None = None
    source_format: str | None = None  # 'csv' or 'ofx'

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
