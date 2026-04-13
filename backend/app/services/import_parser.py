"""CSV transaction parser — auto-detects delimiter and maps ING bank format.

Designed as a pluggable module: add new bank formats by adding new parse
functions. The router picks the right parser based on file content.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass
class ParsedRow:
    """A single parsed transaction row from a bank export file."""

    row_number: int
    date: date
    description: str
    amount: Decimal
    type: str  # "income" or "expense"
    counterparty: str | None = None
    transaction_type: str | None = None  # "Payment terminal", "iDEAL", "Online Banking"
    raw_data: dict = field(default_factory=dict)


class ParseError(Exception):
    """Raised when a CSV file cannot be parsed."""

    def __init__(self, message: str, row_number: int | None = None):
        self.row_number = row_number
        super().__init__(message)


def detect_delimiter(content: str) -> str:
    """Detect CSV delimiter by inspecting the header line.

    ING exports use semicolons (NL locale) or commas (EN locale).
    Semicolons are checked first because comma appears inside quoted fields.
    """
    first_line = content.split("\n", 1)[0]
    if ";" in first_line:
        return ";"
    return ","


def _parse_amount(value: str) -> Decimal:
    """Parse European-format amount: '63,97' -> Decimal('63.97').

    Handles optional thousands separator (period): '1.234,56' -> 1234.56
    """
    cleaned = value.strip().strip('"')
    # European format: period = thousands, comma = decimal
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ParseError(f"Cannot parse amount: '{value}'")


def _parse_date_yyyymmdd(value: str) -> date:
    """Parse ING date format: '20260406' -> date(2026, 4, 6)."""
    cleaned = value.strip().strip('"')
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise ParseError(f"Cannot parse date: '{value}'")
    try:
        return date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:8]))
    except ValueError:
        raise ParseError(f"Invalid date: '{value}'")


def _strip_bom(content: str) -> str:
    """Remove UTF-8 BOM if present (common in Windows CSV exports)."""
    return content.lstrip("\ufeff")


def parse_csv(content: str) -> list[ParsedRow]:
    """Parse an ING-format CSV file (semicolon or comma delimited).

    Expected columns (semicolon variant has 2 extra: Resulting balance, Tag):
      Date | Name / Description | Account | Counterparty | Code |
      Debit/credit | Amount (EUR) | Transaction type | Notifications
      [| Resulting balance | Tag]

    Returns a list of ParsedRow in file order.
    Raises ParseError on structural issues (missing columns, unparseable values).
    """
    content = _strip_bom(content)
    delimiter = detect_delimiter(content)
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

    required_columns = {"Date", "Name / Description", "Debit/credit", "Amount (EUR)"}
    if reader.fieldnames is None:
        raise ParseError("CSV file is empty or has no header row")

    actual_columns = {c.strip().strip('"') for c in reader.fieldnames}
    missing = required_columns - actual_columns
    if missing:
        raise ParseError(f"Missing required columns: {', '.join(sorted(missing))}")

    rows: list[ParsedRow] = []
    for i, row in enumerate(reader, start=1):
        # Normalize keys (strip quotes and whitespace from DictReader keys)
        row = {k.strip().strip('"'): v.strip().strip('"') if v else "" for k, v in row.items()}

        direction = row.get("Debit/credit", "")
        tx_type = "expense" if direction == "Debit" else "income"

        try:
            parsed_date = _parse_date_yyyymmdd(row.get("Date", ""))
            parsed_amount = _parse_amount(row.get("Amount (EUR)", "0"))
        except ParseError as e:
            raise ParseError(str(e), row_number=i)

        description = row.get("Name / Description", "").strip()
        if not description:
            raise ParseError("Empty description", row_number=i)

        rows.append(
            ParsedRow(
                row_number=i,
                date=parsed_date,
                description=description,
                amount=parsed_amount,
                type=tx_type,
                counterparty=row.get("Counterparty", "").strip() or None,
                transaction_type=row.get("Transaction type", "").strip() or None,
                raw_data=row,
            )
        )

    if not rows:
        raise ParseError("CSV file contains no transaction rows")

    return rows
