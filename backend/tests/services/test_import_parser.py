from datetime import date
from decimal import Decimal

import pytest

from app.services.import_parser import ParseError, detect_delimiter, parse_csv


SEMICOLON_CSV = """Date;Name / Description;Account;Counterparty;Code;Debit/credit;Amount (EUR);Transaction type;Notifications
20260406;Groceries;NL00BANK0123456789;Store BV;123;Debit;63,97;Payment terminal;Card payment
20260407;Salary;NL00BANK0123456789;Employer BV;456;Credit;2500,00;Online Banking;Payroll
"""


COMMA_CSV = '''Date,Name / Description,Account,Counterparty,Code,Debit/credit,Amount (EUR),Transaction type,Notifications
20260408,"Dinner, with friends",NL00BANK0123456789,Restaurant,789,Debit,"45,50",Payment terminal,Card payment
'''


def test_detect_delimiter_prefers_semicolon_when_present() -> None:
    assert detect_delimiter(SEMICOLON_CSV) == ";"
    assert detect_delimiter(COMMA_CSV) == ","


def test_parse_csv_supports_semicolon_and_comma_variants() -> None:
    semicolon_rows = parse_csv(SEMICOLON_CSV)
    comma_rows = parse_csv(COMMA_CSV)

    assert len(semicolon_rows) == 2
    assert semicolon_rows[0].row_number == 1
    assert semicolon_rows[0].date == date(2026, 4, 6)
    assert semicolon_rows[0].amount == Decimal("63.97")
    assert semicolon_rows[0].type == "expense"
    assert semicolon_rows[1].amount == Decimal("2500.00")
    assert semicolon_rows[1].type == "income"

    assert len(comma_rows) == 1
    assert comma_rows[0].description == "Dinner, with friends"
    assert comma_rows[0].amount == Decimal("45.50")


def test_parse_csv_raises_on_missing_required_columns() -> None:
    with pytest.raises(ParseError, match="Missing required columns"):
        parse_csv("Date,Name / Description\n20260406,Groceries\n")


def test_parse_csv_surfaces_empty_description_with_row_number() -> None:
    content = """Date;Name / Description;Debit/credit;Amount (EUR)
20260406;;Debit;12,34
"""

    with pytest.raises(ParseError, match="Empty description") as exc:
        parse_csv(content)

    assert exc.value.row_number == 1
