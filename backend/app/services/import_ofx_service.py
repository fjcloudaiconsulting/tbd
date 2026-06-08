"""OFX 1.x / 2.x preview parser (L3.2 Wave 2A).

Wraps the ``ofxtools`` library with three bounds locked by the
L3.2 import contracts (see spec
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
§1):

  1. Hard 5 MB upload cap, enforced *before* parsing begins.
  2. 10-second parse timeout via ``asyncio.wait_for`` so a pathological
     file can't pin an ASGI worker.
  3. 2 000-row hard cap on the parsed transaction list (413 on excess).
     Lowered from 10 000 as a DoS stopgap (see ``MAX_ROWS``).

Isolation rationale: in-process bounded, NOT subprocess. ``ofxtools``
is pure-Python with no native dependencies, uses ``defusedxml`` for
OFX 2.x (XXE protection built in), and parses OFX 1.x SGML in pure
Python. A subprocess-per-upload model would cost more in IPC than the
parse itself and complicate debugging. The three bounds above keep
worst-case CPU / memory predictable.

Output normalization: emits ``ParsedRow`` instances with the OFX
extras (``fitid``, ``bank_id``, ``account_type_ofx``) populated.
``build_preview`` consumes the same ``ParsedRow`` shape regardless of
source format, so the OFX path reuses the duplicate-detection,
transfer-match, and smart-rules pipeline wholesale.

Privacy: never log raw OFX content (account numbers, balances).
``ParseError`` strings carry only the structural failure summary.
"""

from __future__ import annotations

import asyncio
import io
from datetime import date as date_t
from decimal import Decimal

import structlog
from fastapi import HTTPException

from app.services.import_parser import ParseError, ParsedRow

logger = structlog.get_logger()


# ── Spec-locked bounds (L3.2 §1.2 + §1.4) ──
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_TIMEOUT_S = 10.0
MAX_ROWS = 2_000  # DoS stopgap: lowered from 10k to shrink concurrent-parse blast radius


def _coerce_account_type(value: object) -> str | None:
    """Normalize ofxtools ``accttype`` to the contract enum.

    Only values declared on ``ImportPreviewRow.account_type_ofx`` are
    returned; everything else collapses to None so a future bank-specific
    value doesn't break the response schema.
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    if s in ("CHECKING", "SAVINGS", "CREDITLINE", "MONEYMRKT"):
        return s
    return None


def _amount_to_decimal(value: object) -> Decimal:
    """ofxtools yields ``Decimal`` already; this is a defensive wrapper."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_date(value: object) -> date_t:
    """ofxtools returns ``datetime`` (tz-aware). Truncate to date in the
    transaction's own timezone (default UTC per spec §1.3)."""
    if hasattr(value, "date") and callable(value.date):
        return value.date()  # type: ignore[no-any-return]
    if isinstance(value, date_t):
        return value
    raise ParseError(f"Unparseable DTPOSTED: {value!r}")


def _description_for(tx: object) -> str:
    """OFX <NAME> first, fall back to <MEMO>, then placeholder.

    Per spec §1.3 description field mapping.
    """
    name = getattr(tx, "name", None)
    if name:
        return str(name).strip()
    memo = getattr(tx, "memo", None)
    if memo:
        return str(memo).strip()
    return "(no description)"


def _parse_in_executor(raw: bytes) -> object:
    """Synchronous ofxtools parse + convert. Runs inside the
    ``asyncio.wait_for`` timeout via ``run_in_executor``.

    Kept module-private so the timeout wrapper is the only public path.
    """
    # Import locally so the ofxtools dependency stays soft for the rest
    # of the codebase (only loaded when the OFX router actually fires).
    from ofxtools.Parser import OFXTree

    parser = OFXTree()
    parser.parse(io.BytesIO(raw))
    return parser.convert()


async def parse_ofx(
    file_bytes: bytes,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_rows: int = MAX_ROWS,
) -> list[ParsedRow]:
    """Parse an OFX 1.x SGML or 2.x XML file and return ``ParsedRow``s.

    Args:
        file_bytes: Raw upload payload (bytes).
        max_bytes: Hard upload cap; HTTP 413 above this.
        timeout_s: Parse timeout via ``asyncio.wait_for``; HTTP 400 on hit.
        max_rows: Post-parse row cap; HTTP 413 on excess.

    Returns:
        ``list[ParsedRow]`` in the file's natural order, with OFX-specific
        extras (``fitid``, ``bank_id``, ``account_type_ofx``) populated
        from the statement / transaction nodes.

    Raises:
        HTTPException(413): Upload exceeds ``max_bytes`` or post-parse
            row count exceeds ``max_rows``.
        ParseError: Structural failure (malformed file, missing
            ``<TRANLIST>``, unparseable date / amount). The router maps
            this to HTTP 400 via the ``ValidationError`` shim.
        HTTPException(400): Parse exceeded ``timeout_s``.
    """
    # ── (1) Size cap ──
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"OFX file too large ({len(file_bytes)} bytes; "
                f"max {max_bytes // 1024 // 1024} MB). "
                "Split by date range and re-upload."
            ),
        )

    # ── (2) Bounded parse via asyncio.wait_for ──
    loop = asyncio.get_running_loop()
    try:
        ofx = await asyncio.wait_for(
            loop.run_in_executor(None, _parse_in_executor, file_bytes),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        await logger.ainfo(
            "import.ofx.parse.timeout",
            timeout_s=timeout_s,
            bytes=len(file_bytes),
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"OFX file too complex to parse within {int(timeout_s)} seconds; "
                "split into smaller exports."
            ),
        )
    except ParseError:
        # Allow inner ParseError to propagate untouched (router will map).
        raise
    except HTTPException:
        raise
    except Exception as exc:
        # ofxtools raises a family of OFXHeaderError / OFXSpecError types.
        # Surface only the class name + first line of detail to avoid
        # leaking raw file content into logs / HTTP responses.
        message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise ParseError(f"OFX parse failed: {message}")

    # ── (3) Locate the statement / TRANLIST ──
    statements = getattr(ofx, "statements", None) or []
    if not statements:
        raise ParseError("OFX parse failed: no statements found in file.")
    stmt = statements[0]
    transactions = list(getattr(stmt, "transactions", []) or [])

    if not transactions:
        # Spec §1.4: reject files where <TRANLIST> is missing or empty.
        raise ParseError("OFX parse failed: no transactions in <TRANLIST>.")

    # ── (4) Row cap (post-parse) ──
    if len(transactions) > max_rows:
        raise HTTPException(
            status_code=413,
            detail=(
                f"OFX file contains {len(transactions)} transactions; "
                f"max {max_rows}. Split by date range."
            ),
        )

    # ── (5) Account-level extras (per spec §1.3) ──
    account = getattr(stmt, "account", None)
    bank_id_value = getattr(account, "bankid", None) if account is not None else None
    accttype_value = (
        getattr(account, "accttype", None) if account is not None else None
    )
    bank_id = str(bank_id_value).strip() if bank_id_value else None
    account_type_ofx = _coerce_account_type(accttype_value)
    # Credit-card statements use <CCACCTFROM> which carries neither
    # <BANKID> nor <ACCTTYPE>; the implied type is CREDITLINE per the
    # OFX 2.x spec (§11.4.4 credit card aggregate). Infer it from the
    # ofxtools aggregate class name so row metadata still tells the
    # frontend "this row came from a credit card."
    if account_type_ofx is None and account is not None:
        if type(account).__name__ == "CCACCTFROM":
            account_type_ofx = "CREDITLINE"

    # ── (6) Normalize each transaction → ParsedRow ──
    parsed: list[ParsedRow] = []
    skipped = 0
    for i, tx in enumerate(transactions, start=1):
        trnamt = getattr(tx, "trnamt", None)
        dtposted = getattr(tx, "dtposted", None)
        if trnamt is None or dtposted is None:
            skipped += 1
            continue
        try:
            amount_raw = _amount_to_decimal(trnamt)
            row_date = _to_date(dtposted)
        except (ParseError, Exception):
            skipped += 1
            continue

        # Per spec §1.3: type from TRNAMT sign; amount is |TRNAMT|.
        row_type = "income" if amount_raw > 0 else "expense"
        amount_abs = amount_raw if amount_raw > 0 else -amount_raw
        if amount_abs == 0:
            # Zero-amount transactions are skipped (OFX spec §11.4.4
            # allows DTSTART markers as STMTTRN; not real money flow).
            skipped += 1
            continue

        description = _description_for(tx)
        fitid_raw = getattr(tx, "fitid", None)
        fitid = str(fitid_raw).strip() if fitid_raw else None

        # PAYEE.NAME if present (spec §1.3). ofxtools models PAYEE as an
        # optional sub-aggregate; access defensively.
        counterparty = None
        payee = getattr(tx, "payee", None)
        if payee is not None:
            payee_name = getattr(payee, "name", None)
            if payee_name:
                counterparty = str(payee_name).strip()

        trntype_raw = getattr(tx, "trntype", None)
        transaction_type = str(trntype_raw).strip() if trntype_raw else None

        parsed.append(
            ParsedRow(
                row_number=i,
                date=row_date,
                description=description,
                amount=amount_abs,
                type=row_type,
                counterparty=counterparty,
                transaction_type=transaction_type,
                fitid=fitid,
                bank_id=bank_id,
                account_type_ofx=account_type_ofx,
            )
        )

    if not parsed:
        raise ParseError(
            "OFX parse failed: no usable transactions after normalization."
        )

    await logger.ainfo(
        "import.ofx.parsed",
        bytes=len(file_bytes),
        statements=len(statements),
        transactions_in=len(transactions),
        transactions_out=len(parsed),
        skipped=skipped,
    )
    return parsed
