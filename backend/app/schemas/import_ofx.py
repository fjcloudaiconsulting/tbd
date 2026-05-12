"""Pydantic schemas for the OFX transaction import flow (Wave 1 contract).

Frozen per spec at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.

These schemas extend (not replace) the existing CSV preview/confirm flow:
- The OFX preview endpoint returns ``ImportPreviewResponse`` (the CSV one)
  from ``app.schemas.import_schemas``. The OFX-specific fields
  (``fitid``, ``bank_id``, ``account_type_ofx``) live DIRECTLY on
  ``ImportPreviewRow`` / ``ImportConfirmRow`` so OpenAPI exposes them and
  the ``extra="forbid"`` constraint allows them through.
- The OFX confirm path reuses ``POST /api/v1/import/confirm`` and the same
  ``ImportConfirmRow`` shape; the OFX extras ride on echoed fields.

Wave 2 OFX team owns the parser implementation; this file freezes the
OFX-specific request envelope and the diagnostics block. The row-level
schema is the single source of truth in ``import_schemas.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# ── Request envelope ──


class ImportOFXPreviewRequest(BaseModel):
    """OFX preview request (multipart form).

    Note: FastAPI handles multipart-form parsing via separate ``UploadFile``
    + ``Form()`` params on the route; this schema is included for OpenAPI
    documentation and contract testing only. The actual route signature
    uses ``file: UploadFile = File(...), account_id: int = Form(...)``.

    Fields:
        account_id: Target account for the import. Must belong to the
            current user's org.
    """

    account_id: int = Field(gt=0)

    model_config = ConfigDict(extra="forbid")


# ── Row-level OFX fields live on the shared row schemas ──
#
# The three OFX-specific row fields (``fitid``, ``bank_id``,
# ``account_type_ofx``) are declared directly on ``ImportPreviewRow`` and
# ``ImportConfirmRow`` in ``app/schemas/import_schemas.py``. They are
# nullable and default to ``None`` on the CSV path. OpenAPI exposes them
# as part of the existing ``ImportPreviewResponse`` schema component.
#
# This intentional consolidation (vs. a separate ``OFXRowExtras`` model)
# means:
#   1. One row schema, one UI reducer.
#   2. ``extra="forbid"`` on the row models stays — fields are declared.
#   3. OpenAPI exposes the contract surface Wave 2 builds against.
#   4. No model-merging gymnastics at the service layer.


# ── Parser diagnostics (optional Wave 2 add-on) ──


class OFXParseDiagnostics(BaseModel):
    """Diagnostic block returned alongside preview when an OFX file parses
    with non-fatal warnings (e.g., missing FITID, unusual TRNTYPE).

    Reserved for Wave 2 — not populated by the Wave 1 stub. Listed here so
    the frontend reducer can be coded once.

    Fields:
        ofx_version: Detected OFX version (e.g. "1.0.3", "2.0", "2.1.1").
        encoding: Detected file encoding.
        statement_count: Number of ``<STMTTRNRS>`` / ``<CCSTMTTRNRS>``
            blocks in the file. >1 means the file contains multiple
            accounts; only the matching ``account_id`` is imported.
        skipped_count: Rows skipped during parse (e.g., zero-amount
            transactions, malformed dates).
        warnings: Human-readable diagnostic strings (no PII).
    """

    ofx_version: str | None = None
    encoding: str | None = None
    statement_count: int = 0
    skipped_count: int = 0
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
