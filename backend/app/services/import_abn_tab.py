"""ABN AMRO ``.TAB`` transaction parser.

ABN AMRO exports some account flows only as a tab-separated ``.TAB`` file
(no header, exactly 8 fields per line). This module turns that file into
``ParsedRow`` objects for the existing preview → confirm → reconcile
pipeline.

Spec: ``specs/2026-06-09-abn-tab-import.md``.

Reuses ``ParsedRow``, ``ParseError``, ``_parse_amount``,
``_parse_date_yyyymmdd`` and ``_strip_bom`` from :mod:`app.services.import_parser`
(imported, never duplicated). The router decodes the upload bytes
(UTF-8 → cp1252 fallback) before calling :func:`parse_tab`.
"""
from __future__ import annotations

import re

from app.services.import_parser import (
    ParsedRow,
    ParseError,
    _parse_amount,
    _parse_date_yyyymmdd,
    _strip_bom,
)

# DoS stopgap, matches the #417 OFX posture. A non-blank line past the cap
# raises ``ParseError`` rather than parsing unbounded input.
MAX_ROWS = 2000

# ABN AMRO lines have exactly 8 tab-separated fields (spec "File format").
_FIELD_COUNT = 8

# SEPA descriptions are a flat ``/TAG/value/TAG/value/...`` string. These
# are the tags observed in real exports; unknown tags still parse fine
# (any uppercase token between slashes becomes a key).
_SEPA_TAGS = {
    "TRTP",
    "CSID",
    "NAME",
    "MARF",
    "REMI",
    "IBAN",
    "BIC",
    "EREF",
    "ULTD",
    "ORDP",
    "ID",
}

# POS / ATM descriptions split on runs of 2+ spaces. The merchant segment
# carries a trailing ``,PASxxx`` card-token suffix we strip for display.
_MULTISPACE = re.compile(r" {2,}")
_PAS_SUFFIX = re.compile(r",PAS\d+\s*$", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def _collapse_whitespace(value: str) -> str:
    """Collapse all whitespace runs to single spaces and strip the ends."""
    return _WHITESPACE.sub(" ", value).strip()


def parse_abn_description(raw: str) -> tuple[str, str | None, dict]:
    """Parse an ABN AMRO transaction description.

    Returns ``(description, counterparty, extracted_tags)``.

    Three branches (spec "Rich description parser"):

    1. **SEPA** — ``raw`` starts with ``/TRTP/``. Split into ``TAG/value``
       pairs. ``counterparty`` is the ``NAME`` value (or ``None``);
       ``description`` is the ``NAME`` value when present, else the
       whitespace-collapsed raw string. Every parsed tag rides into
       ``extracted_tags`` so ``REMI`` / ``EREF`` / ``IBAN`` detail is never
       lost.
    2. **POS / ATM** — ``raw`` starts with ``BEA, `` (card/POS payment) or
       ``GEA, `` (ATM withdrawal). Split on runs of 2+ spaces. The merchant
       is segment ``[1]`` with the trailing ``,PASxxx`` card suffix
       stripped; it becomes both ``counterparty`` and ``description``.
       Remaining segments (card NR, datetime, city) ride into
       ``extracted_tags``.
    3. **Fallback** — anything else: ``description`` is the
       whitespace-collapsed raw string, ``counterparty`` is ``None``.

    Per the spec's user-approved decision, the clean counterparty name is
    used as BOTH the counterparty and the description (cleaner display +
    stronger deterministic auto-categorization); the full remittance text
    is preserved in ``extracted_tags`` / ``raw_data``.
    """
    raw = raw or ""
    stripped = raw.strip()

    # ── Branch 1: SEPA ──
    if stripped.startswith("/TRTP/"):
        tags: dict[str, str] = {}
        # Walk the ``/``-delimited tokens treating KNOWN tags as the only
        # keys; everything between two tags is that tag's value. Naive
        # every-2-pairing breaks on real exports because (a) iDEAL rows
        # carry an extra unpaired token (``/TRTP/iDEAL/Wero/IBAN/...``)
        # that shifts the alignment so ``NAME`` lands in a value slot, and
        # (b) ``REMI`` values legitimately contain ``/`` (dates, URLs).
        # Tag-as-delimiter handles both. A tag can recur: payment-processor
        # direct debits carry the processor as the first ``NAME`` and the
        # actual merchant as a second ``NAME`` after ``ULTD`` (e.g.
        # ``/NAME/Stichting Pay.nl/.../ULTD//NAME/Impressive Dance Studio's``).
        # Last write wins ON PURPOSE: the trailing NAME is the ultimate
        # creditor, which is the merchant the user recognizes — strictly
        # more useful as the counterparty than the processor. Pinned by
        # test_sepa_double_name_prefers_ultimate_creditor.
        tokens = stripped.split("/")[1:]
        current_key: str | None = None
        buffer: list[str] = []
        for tok in tokens:
            if tok in _SEPA_TAGS:
                if current_key is not None:
                    tags[current_key] = "/".join(buffer).strip()
                current_key = tok
                buffer = []
            else:
                buffer.append(tok)
        if current_key is not None:
            tags[current_key] = "/".join(buffer).strip()

        name = tags.get("NAME", "").strip()
        counterparty = name or None
        description = name if name else _collapse_whitespace(stripped)
        return description, counterparty, tags

    # ── Branch 2: POS / ATM ──
    if stripped.startswith("BEA, ") or stripped.startswith("GEA, "):
        segments = [s.strip() for s in _MULTISPACE.split(stripped) if s.strip()]
        kind = "pos" if stripped.startswith("BEA,") else "atm"
        merchant = ""
        if len(segments) > 1:
            merchant = _PAS_SUFFIX.sub("", segments[1]).strip()
        extracted: dict = {"abn_pos_kind": kind, "abn_pos_segments": segments}
        if merchant:
            return merchant, merchant, extracted
        # No merchant segment — degrade to the fallback display but keep
        # the parsed segments.
        return _collapse_whitespace(stripped), None, extracted

    # ── Branch 3: fallback ──
    return _collapse_whitespace(stripped), None, {}


def parse_tab(content: str) -> list[ParsedRow]:
    """Parse an ABN AMRO ``.TAB`` (tab-separated, no header) export.

    Each non-blank line has exactly 8 tab fields (spec "File format"):

      0. Account number   → ``raw_data`` (user picks the account in the UI)
      1. Currency         → ``raw_data`` (informational)
      2. Book date YYYYMMDD → ``ParsedRow.date``
      3. Balance before   → ``raw_data``
      4. Balance after    → ``raw_data``
      5. Value date YYYYMMDD → ``raw_data``
      6. Signed amount    → sign drives ``type`` (expense/income); ``abs`` → ``amount``
      7. Description       → :func:`parse_abn_description` → ``description`` + ``counterparty``

    Blank lines are skipped. A non-blank line whose field count is not 8
    raises ``ParseError(..., row_number=i)`` (mirrors the CSV path). The
    ``MAX_ROWS`` cap and an empty file both raise ``ParseError``.

    Spec: ``specs/2026-06-09-abn-tab-import.md``.
    """
    content = _strip_bom(content)
    # ABN exports are CRLF; normalize so blank-line detection is uniform.
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    rows: list[ParsedRow] = []
    row_number = 0
    for line in lines:
        if not line.strip():
            continue  # skip blank lines

        row_number += 1
        if row_number > MAX_ROWS:
            raise ParseError(
                f"File exceeds the maximum of {MAX_ROWS} transactions",
                row_number=row_number,
            )

        fields = line.split("\t")
        if len(fields) != _FIELD_COUNT:
            raise ParseError(
                f"Expected {_FIELD_COUNT} tab-separated fields, got {len(fields)}",
                row_number=row_number,
            )

        (
            account_number,
            currency,
            book_date,
            balance_before,
            balance_after,
            value_date,
            amount_raw,
            description_raw,
        ) = (f.strip() for f in fields)

        try:
            parsed_date = _parse_date_yyyymmdd(book_date)
            signed_amount = _parse_amount(amount_raw)
        except ParseError as exc:
            raise ParseError(str(exc), row_number=row_number)

        tx_type = "expense" if signed_amount < 0 else "income"
        amount = abs(signed_amount)
        if amount == 0:
            # No money moved (ABN emits zero-amount balance-marker /
            # reversal rows). Skip rather than import a zero transaction
            # or trip the confirm-time ``amount > 0`` guard. Mirrors the
            # OFX parser (import_ofx_service.py).
            continue

        description, counterparty, extracted_tags = parse_abn_description(
            description_raw
        )
        if not description:
            # A blank description column means a malformed/marker line.
            # Fail loud (mirrors parse_csv) rather than silently importing
            # a nameless transaction — losing a row silently is worse for a
            # finance import than a clear, locatable error.
            raise ParseError("Empty description", row_number=row_number)

        raw_data: dict = {
            "account_number": account_number,
            "currency": currency,
            "book_date": book_date,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "value_date": value_date,
            "amount": amount_raw,
            "description": description_raw,
            **extracted_tags,
        }

        rows.append(
            ParsedRow(
                row_number=row_number,
                date=parsed_date,
                description=description,
                amount=amount,
                type=tx_type,
                counterparty=counterparty,
                raw_data=raw_data,
            )
        )

    if not rows:
        raise ParseError("TAB file contains no transaction rows")

    return rows
