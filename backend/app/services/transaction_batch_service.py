"""Manual batch transaction entry service (L3.2 Wave 2A).

Implements ``POST /api/v1/transactions/batch``. Rows are user-typed (not
imported from a bank file) and therefore land with ``is_imported=False``.
Each row runs inside its own ``db.begin_nested()`` savepoint so a single
failing row doesn't take down the rest of the batch — failures are
collected into a per-row error list and returned to the caller.

The endpoint sits outside the import router by design (see spec
§0.2 in ``2026-05-12-l3-2-import-contracts.md``): manual batch entry
doesn't go through file preview, transfer detection, or
duplicate-of-linked-leg checks. Per-row validation mirrors the
single-row ``create_transaction`` path (account + category org-scope,
category-type compatibility, amount/date bounds enforced by the
shared ``TransactionCreate`` schema).

Smart-rules learning runs best-effort AFTER each successful row's
savepoint commits — mirroring the single-row create path — so a
learn failure on row N never poisons rows N+1..M.
"""
from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.import_batch import (
    BatchRowError,
    BatchRowResult,
    BatchTransactionsRequest,
    BatchTransactionsResponse,
)
from app.services import transaction_service
from app.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.services.category_rules_service import learn_from_choice

logger = structlog.stdlib.get_logger()


def _error_message(exc: Exception) -> str:
    """Render a service-layer exception as a flat human-readable string.

    The domain exceptions (``ValidationError``, ``NotFoundError``,
    ``ConflictError``) carry either a ``detail`` attribute or a plain
    ``args[0]`` message; normalize them so the response payload stays
    a flat ``{"row_number": int, "error": str}`` shape. Unexpected
    exceptions surface as a generic message so we never leak SQL
    state or stack frames to the client.
    """
    if isinstance(exc, (ValidationError, ConflictError)):
        detail = getattr(exc, "detail", None)
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
        return str(exc) or exc.__class__.__name__
    if isinstance(exc, NotFoundError):
        return str(exc) or "not found"
    return "Internal error processing row"


async def create_batch(
    db: AsyncSession,
    org_id: int,
    body: BatchTransactionsRequest,
) -> BatchTransactionsResponse:
    """Process a manual batch entry request.

    Per-row contract:
      * Each row gets its own ``db.begin_nested()`` savepoint.
      * Validation errors / conflicts / not-found errors are caught,
        the savepoint rolls back, and a ``BatchRowError`` is appended.
      * Unexpected exceptions are logged and surface as a generic
        per-row error (never as a 500 — one bad row must not kill
        the batch).
      * Successful rows are flushed inside the savepoint, then the
        savepoint commits and the row's ID is appended to ``results``.

    Smart-rules learning runs OUTSIDE the per-row savepoint after each
    successful insert, best-effort, to match the single-row create
    semantics. A learn failure is logged but never bubbles up.

    The caller (the router) owns the outer transaction's final
    ``db.commit()``. Returning the response object signals success;
    the router commits once at the end so all surviving rows land
    atomically from the client's perspective.
    """
    results: list[BatchRowResult] = []
    errors: list[BatchRowError] = []

    for row in body.rows:
        try:
            async with db.begin_nested():
                tx = await transaction_service._create_transaction_no_commit(
                    db,
                    org_id,
                    row.transaction,
                    is_imported=False,
                )
            results.append(
                BatchRowResult(row_number=row.row_number, transaction_id=tx.id)
            )
        except (ValidationError, ConflictError, NotFoundError) as exc:
            errors.append(
                BatchRowError(
                    row_number=row.row_number,
                    error=_error_message(exc),
                )
            )
        except Exception as exc:  # noqa: BLE001 — defensive: log + continue.
            await logger.aerror(
                "transactions.batch.row_unexpected_error",
                org_id=org_id,
                row_number=row.row_number,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            errors.append(
                BatchRowError(
                    row_number=row.row_number,
                    error=_error_message(exc),
                )
            )

    # Best-effort smart-rules learning per committed row. Each learn
    # runs in its OWN savepoint so a learn failure only rolls back its
    # own staged rule (not the row it would have learned from, and
    # never another row). This mirrors the single-row create path's
    # try/except-around-learn semantics while staying compatible with
    # the outer transaction the router will commit at the end.
    for result in results:
        try:
            async with db.begin_nested():
                tx = await transaction_service.get_transaction(
                    db, org_id, result.transaction_id
                )
                await learn_from_choice(
                    db,
                    org_id=org_id,
                    description=tx.description,
                    category_id=tx.category_id,
                    source="user_edit",
                )
        except Exception as exc:  # noqa: BLE001 — best effort.
            await logger.awarning(
                "transactions.batch.learn_failed",
                org_id=org_id,
                row_id=result.transaction_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    return BatchTransactionsResponse(
        imported_count=len(results),
        error_count=len(errors),
        results=results,
        errors=errors,
    )
