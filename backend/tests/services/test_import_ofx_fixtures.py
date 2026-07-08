"""L3.2 §6.2 OFX fixture coverage (Wave 2A).

This module is the regression gate for the six synthetic OFX fixtures
listed in the L3.2 spec §6.2. Each fixture exercises a distinct parser
code path:

  * ``rabobank_ofx_1x.ofx``  - OFX 1.x SGML, vanilla checking statement
  * ``ing_ofx_2x.ofx``       - OFX 2.x XML, vanilla checking statement
  * ``chase_credit_card_2x.ofx`` - OFX 2.x XML with ``CCSTMTRS`` (credit
    card) section instead of ``STMTRS`` (deposit account). Validates the
    ``CCACCTFROM`` shape where ``bankid`` may be absent.
  * ``malformed_truncated.ofx`` - structurally invalid OFX 1.x;
    must surface as ``ParseError`` (HTTP 400 at the router boundary).
  * ``large_10k_rows.ofx``  - 10 000-row OFX 2.x XML, the production-
    supported maximum (spec §1.4). Kept on disk as a fixture so the
    inventory regression gate stays green, but NOT exercised by a
    correctness or perf test in this module: the 10k intake contract
    is being decided separately (see
    ``memory/project_next_dev_batch_2026_05_15.md`` → "OFX parser
    scale / isolation contract").
  * ``quicken_qfx.qfx``      - OFX 1.x ``.qfx`` Quicken variant with the
    INTU.BID extension; validates the ``.qfx`` extension is parsed by
    the same code path as ``.ofx`` 1.x SGML.

All fixtures are synthetic per spec §6.2: fictional account numbers
(``NL01TEST...``, ``4111TESTCC...``, etc.) and fictional merchants.
NO real bank data anywhere in this tree.

Tests are intentionally service-layer (``parse_ofx`` direct calls)
rather than router-layer. The router contract is covered exhaustively
by ``backend/tests/routers/test_import_ofx.py``; this module asserts
that the parser itself handles every fixture variant without a HTTP
shell. That way, when the OFX service gets future hardening (e.g. a
new dialect), one test failure points at the parser code, not the
router wiring.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.services import import_ofx_service
from app.services.import_ofx_service import parse_ofx
from app.services.import_parser import ParseError, ParsedRow


FIXTURES = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "import"
    / "ofx"
)


def _read(name: str) -> bytes:
    """Read a fixture file as bytes."""
    return (FIXTURES / name).read_bytes()


# ── rabobank_ofx_1x.ofx: 15-row OFX 1.x SGML checking statement ─────────────


@pytest.mark.asyncio
async def test_rabobank_ofx_1x_parses_to_15_rows():
    """OFX 1.x SGML produces exactly 15 ``ParsedRow``s with FITIDs."""
    rows = await parse_ofx(_read("rabobank_ofx_1x.ofx"))
    assert len(rows) == 15
    # Every row carries the FITID extension and the bank/account-type
    # extras (spec §1.3).
    for row in rows:
        assert isinstance(row, ParsedRow)
        assert row.fitid is not None
        assert row.fitid.startswith("RABO")
        assert row.bank_id == "RABONL2U"
        assert row.account_type_ofx == "CHECKING"
    # First row is the canonical -12.50 EUR Albert Heijn debit.
    first = rows[0]
    assert first.type == "expense"
    assert first.amount == Decimal("12.50")
    assert first.description == "Albert Heijn"
    assert first.fitid == "RABO000001"
    # Salary row is income.
    salary = next(r for r in rows if r.fitid == "RABO000003")
    assert salary.type == "income"
    assert salary.amount == Decimal("2500.00")


# ── ing_ofx_2x.ofx: 25-row OFX 2.x XML checking statement ───────────────────


@pytest.mark.asyncio
async def test_ing_ofx_2x_parses_to_25_rows():
    """OFX 2.x XML produces exactly 25 ``ParsedRow``s with FITIDs."""
    rows = await parse_ofx(_read("ing_ofx_2x.ofx"))
    assert len(rows) == 25
    for row in rows:
        assert row.fitid is not None
        assert row.fitid.startswith("ING")
        assert row.bank_id == "INGBNL2A"
        assert row.account_type_ofx == "CHECKING"
    # Income rows present (salary + refund).
    income_rows = [r for r in rows if r.type == "income"]
    assert len(income_rows) == 2


# ── chase_credit_card_2x.ofx: 20-row CCSTMTRS section ───────────────────────


@pytest.mark.asyncio
async def test_chase_credit_card_2x_parses_cc_statement():
    """A ``CCSTMTRS`` (credit card) statement parses as 20 ``ParsedRow``s.

    Validates that the parser correctly handles ``CCACCTFROM`` (which
    has no ``<BANKID>``, only ``<ACCTID>`` and the implied account
    type). For credit-card statements ofxtools exposes the account
    object with ``bankid`` absent / None, so ``bank_id`` on the
    ``ParsedRow`` is also None. ``account_type_ofx`` resolves to
    ``CREDITLINE`` per the contract.
    """
    rows = await parse_ofx(_read("chase_credit_card_2x.ofx"))
    assert len(rows) == 20
    # Credit card statements have no <BANKID>; ``bank_id`` is None.
    # ofxtools surfaces credit card statements with ``accttype`` set to
    # CREDITLINE on its synthetic account aggregate.
    for row in rows:
        assert row.fitid is not None
        assert row.fitid.startswith("CHASE")
        assert row.bank_id is None
        assert row.account_type_ofx == "CREDITLINE"
    # Sign mapping: refunds and AUTOPAY are income, charges are expense.
    autopay = next(r for r in rows if r.fitid == "CHASE000011")
    assert autopay.type == "income"
    assert autopay.amount == Decimal("425.00")
    refund = next(r for r in rows if r.fitid == "CHASE000015")
    assert refund.type == "income"
    assert refund.amount == Decimal("32.18")
    # Expense rows are 18 out of 20 (two CREDIT rows above).
    expense_rows = [r for r in rows if r.type == "expense"]
    assert len(expense_rows) == 18


# ── malformed_truncated.ofx: parse must fail with ParseError ────────────────


@pytest.mark.asyncio
async def test_malformed_truncated_raises_parse_error():
    """A structurally invalid OFX file surfaces as ``ParseError``.

    The router maps ``ParseError`` to HTTP 400 via the
    ``ValidationError`` shim. This test pins the service-layer
    contract: any structural anomaly raises ``ParseError`` with a
    human-readable summary (no stack trace, no raw file content).
    """
    with pytest.raises(ParseError) as exc_info:
        await parse_ofx(_read("malformed_truncated.ofx"))
    message = str(exc_info.value)
    # Detail must lead with "OFX parse failed:" so the router's 400
    # response is recognizable to the frontend.
    assert "OFX parse failed" in message
    # No raw account numbers leaked into the error string.
    assert "NL01TEST" not in message
    assert "Traceback" not in message


# ── Parser timeout contract (deterministic, monkeypatched) ──────────────────
#
# NOTE: ``large_10k_rows.ofx`` is intentionally NOT exercised by a
# correctness or perf test in this module. The 10 000-row intake
# contract (does ``parse_ofx`` complete within the 10s production
# budget?) is being decided in a separate follow-up — see
# ``memory/project_next_dev_batch_2026_05_15.md`` → "OFX parser scale /
# isolation contract." The fixture itself stays on disk so the
# ``test_all_required_fixtures_present_on_disk`` regression gate stays
# green and the follow-up doesn't have to re-create it.


@pytest.mark.asyncio
async def test_ofx_parser_enforces_production_timeout(monkeypatch, tmp_path):
    """Spec §1.4 timeout contract: an overrunning parse raises HTTP 400.

    Post-isolation, parsing runs in a hard-killable child process. The
    ``PFV_OFX_TEST_HANG_S`` worker seam spins that child in a real CPU
    busy-loop; ``parse_ofx`` must terminate it once ``timeout_s`` elapses
    and raise HTTP 400. The sentinel proves the runaway was killed, not
    merely abandoned.

    The router-layer mirror of this contract is
    ``test_ofx_preview_timeout_returns_400`` in
    ``backend/tests/routers/test_import_ofx.py``; this service-layer
    test pins the same contract one level lower.
    """
    from fastapi import HTTPException

    sentinel = tmp_path / "completed.flag"
    monkeypatch.setenv("PFV_OFX_TEST_HANG_S", "10")
    monkeypatch.setenv("PFV_OFX_TEST_SENTINEL", str(sentinel))

    with pytest.raises(HTTPException) as exc_info:
        await parse_ofx(b"<OFX>stub</OFX>", timeout_s=0.5)
    assert exc_info.value.status_code == 400
    detail = str(exc_info.value.detail).lower()
    assert "complex" in detail or "seconds" in detail
    # Hard-killed: worker never completed its busy-loop.
    assert not sentinel.exists()


# ── quicken_qfx.qfx: 12-row OFX 1.x .qfx variant ────────────────────────────


@pytest.mark.asyncio
async def test_quicken_qfx_parses_through_ofx_1x_path():
    """The ``.qfx`` Quicken variant parses through the OFX 1.x SGML path.

    QFX is OFX 1.x SGML with optional ``<INTU.BID>`` / ``<INTU.USERID>``
    Quicken extensions in the SONRS block. ``ofxtools`` accepts these
    extensions and surfaces the rest of the statement identically to
    an ``.ofx`` 1.x file. This test pins that contract: the same
    ``parse_ofx`` entry point handles both extensions, no special
    branching at the service layer.
    """
    rows = await parse_ofx(_read("quicken_qfx.qfx"))
    assert len(rows) == 12
    for row in rows:
        assert row.fitid is not None
        assert row.fitid.startswith("QFX")
        # The QFX fixture is a savings account; account_type_ofx must
        # surface as SAVINGS, not CHECKING.
        assert row.account_type_ofx == "SAVINGS"
        assert row.bank_id == "123456789"
    # Direct deposit row is income.
    payroll = next(r for r in rows if r.fitid == "QFX00000001")
    assert payroll.type == "income"
    assert payroll.amount == Decimal("1500.00")
    # XFER (transfer-leg) rows are still parsed; the transfer-detector
    # runs at a later stage (build_preview), not in the OFX parser.
    transfer = next(r for r in rows if r.fitid == "QFX00000006")
    assert transfer.type == "expense"
    assert transfer.amount == Decimal("200.00")


# ── Fixture inventory regression gate ───────────────────────────────────────


def test_all_required_fixtures_present_on_disk():
    """All six spec §6.2 fixtures must be committed to the repo.

    This is the regression gate that prevents a future cleanup from
    accidentally dropping one of the synthetic files. Each fixture
    name and the spec §6.2 row it backs must stay in lockstep.
    """
    required = [
        "rabobank_ofx_1x.ofx",
        "ing_ofx_2x.ofx",
        "chase_credit_card_2x.ofx",
        "malformed_truncated.ofx",
        "large_10k_rows.ofx",
        "quicken_qfx.qfx",
    ]
    missing = [name for name in required if not (FIXTURES / name).exists()]
    assert not missing, f"L3.2 §6.2 fixtures missing from disk: {missing}"
