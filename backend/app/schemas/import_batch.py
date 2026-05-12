"""Pydantic schemas for manual batch transaction entry (Wave 1 contract).

Frozen per spec at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.

This endpoint is OUTSIDE the import router. Manual batch entry lets a user
type N transactions at once (e.g., bulk-receipting a week of cash receipts)
without going through file upload, preview, or transfer-detection. It's a
sibling of ``POST /api/v1/transactions`` that accepts a list and returns
per-row results.

Key differences vs. import-confirm:
- No ``is_imported=True`` flag — these are user-typed, not bank-sourced.
- No preview step; the user submits final values directly.
- No transfer-detection or duplicate-of-linked-leg check.
- No smart-rules learning (the existing single-create path already covers
  that via ``transaction_service.create_transaction``; batch entry runs the
  same per-row logic, so learning still happens row-by-row).
- Reuses ``TransactionCreate`` for the row body — same validation as the
  single-row endpoint.

Wave 2 Manual Batch Entry team owns the implementation. This file freezes
the request/response wire shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.transaction import TransactionCreate


class BatchTransactionRow(BaseModel):
    """One row in a manual batch entry submission.

    Wraps ``TransactionCreate`` with a stable ``row_number`` so the
    response can map per-row results back to the user's input order.

    Fields:
        row_number: Client-provided row index (1-based by convention).
            Echoed back in the response for client-side error mapping.
            Must be unique within a single request — server returns 422
            on duplicate ``row_number``.
        transaction: The transaction to create. Same validation as the
            single-row ``POST /api/v1/transactions`` endpoint.
    """

    row_number: int = Field(ge=1)
    transaction: TransactionCreate

    model_config = ConfigDict(extra="forbid")


class BatchTransactionsRequest(BaseModel):
    """Request body for ``POST /api/v1/transactions/batch``.

    Server processes each row in its own savepoint (mirroring the
    import-confirm pattern in ``import_service.execute_import``) so a
    single failing row doesn't take down the batch. Returns per-row
    errors plus aggregate counters.

    Fields:
        rows: Ordered list of rows to insert. Capped at 500 rows per
            request to bound request size and per-row savepoint
            overhead. Frontend chunks larger batches client-side.
    """

    rows: list[BatchTransactionRow] = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _row_numbers_must_be_unique(self) -> "BatchTransactionsRequest":
        """Reject payloads with duplicate ``row_number`` values.

        The response shape maps per-row results back to the user's input
        order via ``row_number``; duplicates would collide and make
        error reporting ambiguous. Pydantic converts the raised
        ``ValueError`` into a 422 with the field locator pointing at
        ``rows`` (and the duplicate values surfaced in the message).
        """
        seen: set[int] = set()
        duplicates: list[int] = []
        for row in self.rows:
            if row.row_number in seen:
                duplicates.append(row.row_number)
            else:
                seen.add(row.row_number)
        if duplicates:
            raise ValueError(
                "row_number values must be unique across rows; "
                f"duplicates: {sorted(set(duplicates))}"
            )
        return self


class BatchRowError(BaseModel):
    """Error detail for a single row that failed during batch insert.

    Fields:
        row_number: The ``row_number`` from the request row, echoed back.
        error: Human-readable error message. Same shape as the domain
            ``ValidationError.detail`` / ``ConflictError.detail`` returned
            by the service layer.
    """

    row_number: int
    error: str

    model_config = ConfigDict(extra="forbid")


class BatchRowResult(BaseModel):
    """Success record for a single row that imported cleanly.

    Fields:
        row_number: The ``row_number`` from the request row.
        transaction_id: ID of the created transaction.
    """

    row_number: int
    transaction_id: int

    model_config = ConfigDict(extra="forbid")


class BatchTransactionsResponse(BaseModel):
    """Response body for ``POST /api/v1/transactions/batch``.

    Counter invariant:
        ``imported_count + error_count == len(request.rows)``

    Fields:
        imported_count: Rows that committed successfully.
        error_count: Rows that failed (sum of errors below).
        results: Per-row success records (in input order).
        errors: Per-row error records (in input order).
    """

    imported_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    results: list[BatchRowResult] = Field(default_factory=list)
    errors: list[BatchRowError] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
