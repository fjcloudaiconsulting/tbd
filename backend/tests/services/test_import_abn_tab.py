"""ABN AMRO ``.TAB`` parser unit tests.

Covers ``parse_tab`` and ``parse_abn_description`` in
``app.services.import_abn_tab``: 8-column mapping, sign → type, SEPA
``NAME`` → counterparty, POS ``BEA`` / ATM ``GEA`` merchant extraction +
``,PASxxx`` strip, fallback branch, malformed line (wrong field count)
raising with ``row_number``, European amount + ``YYYYMMDD`` date, cp1252
decode, ``MAX_ROWS`` cap, and empty file.

Spec: ``specs/2026-06-09-abn-tab-import.md``.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.import_abn_tab import MAX_ROWS, parse_abn_description, parse_tab
from app.services.import_parser import ParseError


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "import" / "tab"


def _line(*fields: str) -> str:
    assert len(fields) == 8
    return "\t".join(fields)


# ── parse_abn_description: SEPA branch ──


def test_sepa_name_becomes_counterparty_and_description() -> None:
    raw = "/TRTP/SEPA Incasso/NAME/WATER COMPANY BV/REMI/Invoice 42/IBAN/NL00TEST0123456789"
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty == "WATER COMPANY BV"
    assert description == "WATER COMPANY BV"
    # All parsed tags preserved so REMI / IBAN detail is never lost.
    assert tags["TRTP"] == "SEPA Incasso"
    assert tags["REMI"] == "Invoice 42"
    assert tags["IBAN"] == "NL00TEST0123456789"


def test_sepa_ideal_wero_extracts_name_despite_extra_token() -> None:
    # iDEAL rows carry an unpaired ``Wero`` token (/TRTP/iDEAL/Wero/IBAN/...)
    # that shifts naive every-2 pairing so NAME lands in a value slot.
    # Tag-as-delimiter parsing must still find the real counterparty.
    raw = (
        "/TRTP/iDEAL/Wero/IBAN/NL04TEST2017400157/BIC/ADYBNL2A/"
        "NAME/DHL eCommerce .Services. B.V./REMI/order ref/EREF/2026"
    )
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty == "DHL eCommerce .Services. B.V."
    assert description == "DHL eCommerce .Services. B.V."
    assert tags["TRTP"] == "iDEAL/Wero"


def test_sepa_remi_value_with_slashes_preserved_and_alignment_kept() -> None:
    # REMI values legitimately contain slashes (date ranges, URLs). The
    # value must be kept whole AND later tags must stay correctly aligned.
    raw = (
        "/TRTP/SEPA Incasso/CSID/NL47ZZZ370924590000/NAME/Basic Fit B.V./"
        "MARF/C31001922/REMI/23-02-2026 / 22-03-2026/IBAN/NL24TEST0168476207/"
        "BIC/RABONL2U/EREF/NOC31001922-0141"
    )
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty == "Basic Fit B.V."
    assert tags["REMI"] == "23-02-2026 / 22-03-2026"
    assert tags["IBAN"] == "NL24TEST0168476207"
    assert tags["EREF"] == "NOC31001922-0141"


def test_sepa_without_name_falls_back_to_collapsed_raw() -> None:
    raw = "/TRTP/SEPA Overboeking/REMI/some reference text"
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty is None
    assert description == "/TRTP/SEPA Overboeking/REMI/some reference text"
    assert tags["REMI"] == "some reference text"


# ── parse_abn_description: POS / ATM branch ──


def test_bea_pos_merchant_extracted_with_pas_suffix_stripped() -> None:
    raw = "BEA, Betaalpas   SUPERMARKET LOC 529,PAS523   NR:A1B2C3, 21.02.26/18:57   AMERSFOORT"
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty == "SUPERMARKET LOC 529"
    assert description == "SUPERMARKET LOC 529"
    assert tags["abn_pos_kind"] == "pos"
    assert "AMERSFOORT" in tags["abn_pos_segments"]


def test_gea_atm_merchant_extracted() -> None:
    raw = "GEA, Geldautomaat   ABN AMRO LOC 412,PAS523   NR:Z9Y8X7, 22.02.26/09:13   UTRECHT"
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty == "ABN AMRO LOC 412"
    assert description == "ABN AMRO LOC 412"
    assert tags["abn_pos_kind"] == "atm"


# ── parse_abn_description: fallback branch ──


def test_fallback_collapses_whitespace_no_counterparty() -> None:
    raw = "ABN AMRO Bank N.V.            Maandelijkse kosten betaalpakket"
    description, counterparty, tags = parse_abn_description(raw)
    assert counterparty is None
    assert description == "ABN AMRO Bank N.V. Maandelijkse kosten betaalpakket"
    assert tags == {}


# ── parse_tab: column mapping + sign → type + European amount / date ──


def test_parse_tab_maps_columns_and_sign() -> None:
    content = "\r\n".join(
        [
            _line(
                "845455273", "EUR", "20260220", "1043,33", "1031,62", "20260220",
                "-11,71", "/TRTP/SEPA Incasso/NAME/WATER COMPANY BV",
            ),
            _line(
                "845455273", "EUR", "20260225", "940,12", "3440,12", "20260225",
                "2500,00", "/TRTP/SEPA Overboeking/NAME/EXAMPLE EMPLOYER NV",
            ),
        ]
    )
    rows = parse_tab(content)
    assert len(rows) == 2

    debit = rows[0]
    assert debit.row_number == 1
    assert debit.date == date(2026, 2, 20)
    assert debit.amount == Decimal("11.71")  # abs of European-format -11,71
    assert debit.type == "expense"
    assert debit.counterparty == "WATER COMPANY BV"
    assert debit.description == "WATER COMPANY BV"
    # Balance / currency / account / value-date retained in raw_data.
    assert debit.raw_data["currency"] == "EUR"
    assert debit.raw_data["account_number"] == "845455273"
    assert debit.raw_data["balance_after"] == "1031,62"
    assert debit.raw_data["value_date"] == "20260220"

    credit = rows[1]
    assert credit.amount == Decimal("2500.00")
    assert credit.type == "income"
    assert credit.counterparty == "EXAMPLE EMPLOYER NV"


def test_parse_tab_handles_lf_only_line_endings() -> None:
    content = _line(
        "845455273", "EUR", "20260220", "0,00", "0,00", "20260220", "-1,00", "x"
    ) + "\n"
    rows = parse_tab(content)
    assert len(rows) == 1
    assert rows[0].type == "expense"


# ── parse_tab: skips blank lines ──


def test_parse_tab_skips_blank_lines() -> None:
    good = _line(
        "845455273", "EUR", "20260220", "0,00", "0,00", "20260220", "-1,00", "x"
    )
    content = f"\r\n{good}\r\n\r\n{good}\r\n"
    rows = parse_tab(content)
    assert len(rows) == 2
    assert [r.row_number for r in rows] == [1, 2]


# ── parse_tab: malformed line (wrong field count) raises with row_number ──


def test_parse_tab_wrong_field_count_raises_with_row_number() -> None:
    good = _line(
        "845455273", "EUR", "20260220", "0,00", "0,00", "20260220", "-1,00", "x"
    )
    bad = "only\tthree\tfields"
    content = f"{good}\r\n{bad}\r\n"
    with pytest.raises(ParseError) as exc:
        parse_tab(content)
    assert exc.value.row_number == 2
    assert "tab-separated fields" in str(exc.value)


def test_parse_tab_bad_date_raises_with_row_number() -> None:
    bad = _line(
        "845455273", "EUR", "2026XX20", "0,00", "0,00", "20260220", "-1,00", "x"
    )
    with pytest.raises(ParseError) as exc:
        parse_tab(bad)
    assert exc.value.row_number == 1


def test_parse_tab_bad_amount_raises_with_row_number() -> None:
    bad = _line(
        "845455273", "EUR", "20260220", "0,00", "0,00", "20260220", "not-a-number", "x"
    )
    with pytest.raises(ParseError) as exc:
        parse_tab(bad)
    assert exc.value.row_number == 1
    assert "amount" in str(exc.value).lower()


# ── parse_tab: empty file ──


def test_parse_tab_empty_file_raises() -> None:
    with pytest.raises(ParseError, match="no transaction rows"):
        parse_tab("")
    with pytest.raises(ParseError, match="no transaction rows"):
        parse_tab("\r\n\r\n   \r\n")


# ── parse_tab: MAX_ROWS cap ──


def test_parse_tab_enforces_max_rows_cap() -> None:
    line = _line(
        "845455273", "EUR", "20260220", "0,00", "0,00", "20260220", "-1,00", "x"
    )
    over = "\r\n".join([line] * (MAX_ROWS + 1))
    with pytest.raises(ParseError) as exc:
        parse_tab(over)
    assert exc.value.row_number == MAX_ROWS + 1
    assert str(MAX_ROWS) in str(exc.value)


def test_parse_tab_at_row_cap_is_accepted() -> None:
    line = _line(
        "845455273", "EUR", "20260220", "0,00", "0,00", "20260220", "-1,00", "x"
    )
    at_cap = "\r\n".join([line] * MAX_ROWS)
    rows = parse_tab(at_cap)
    assert len(rows) == MAX_ROWS


# ── cp1252 decode (router decodes; parser sees str) ──


def test_parse_tab_handles_cp1252_decoded_accents() -> None:
    # Simulate the router's cp1252 fallback: bytes with a cp1252-only
    # accent (é = 0xE9) decoded with cp1252 reach parse_tab as a str.
    raw_bytes = (
        "845455273\tEUR\t20260220\t0,00\t0,00\t20260220\t-9,99\t"
        "BEA, Betaalpas   CAF\xe9 DE FERME   NR:1, 20.02.26/10:00   PARIS"
    ).encode("cp1252")
    content = raw_bytes.decode("cp1252")
    rows = parse_tab(content)
    assert rows[0].counterparty == "CAFé DE FERME"


# ── fixture happy path ──


def test_parse_tab_fixture_full_coverage() -> None:
    content = (FIXTURES / "abn_sample.tab").read_bytes().decode("utf-8")
    rows = parse_tab(content)
    assert len(rows) == 5

    # SEPA debit, POS, ATM, SEPA credit, fallback.
    assert rows[0].type == "expense"
    assert rows[0].counterparty == "WATER COMPANY BV"
    assert rows[1].counterparty == "SUPERMARKET LOC 529"
    assert rows[2].counterparty == "ABN AMRO LOC 412"
    assert rows[3].type == "income"
    assert rows[3].counterparty == "EXAMPLE EMPLOYER NV"
    assert rows[4].counterparty is None
    assert "Maandelijkse kosten" in rows[4].description
