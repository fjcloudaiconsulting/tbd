"""BE-08: the import confirm body is bounded.

``rows`` used to be an unbounded list (the confirm runs one transaction with
a savepoint per row) and ``description`` an unbounded string headed for the
String(255) ``transactions.description`` column.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.config import Settings
from app.schemas.import_schemas import MAX_CONFIRM_ROWS, ImportConfirmRequest
from app.services.import_parser import MAX_DESCRIPTION_LENGTH, MAX_ROWS


def _row(n: int, description: str = "Albert Heijn") -> dict:
    return {
        "row_number": n, "date": "2026-04-02", "description": description,
        "amount": "12.50", "type": "expense", "skip": True,
    }


def _body(rows: list[dict]) -> dict:
    return {
        "account_id": 1, "default_category_id": 1, "file_name": "bank.ofx",
        "source_format": "ofx", "rows": rows,
    }


def test_confirm_accepts_the_largest_preview_and_rejects_one_more_row():
    ImportConfirmRequest.model_validate(_body([_row(i) for i in range(MAX_CONFIRM_ROWS)]))
    with pytest.raises(PydanticValidationError) as exc:
        ImportConfirmRequest.model_validate(
            _body([_row(i) for i in range(MAX_CONFIRM_ROWS + 1)])
        )
    assert exc.value.errors()[0]["loc"] == ("rows",)
    assert exc.value.errors()[0]["type"] == "too_long"


def test_confirm_cap_covers_every_preview_a_parser_can_return():
    """FENCE. A confirm must fit any preview: the CSV/TAB cap and the OFX
    default cap. Wrong implementation: aligning the confirm with the 2000-row
    text cap, which makes every OFX preview over 2000 rows unconfirmable."""
    assert MAX_CONFIRM_ROWS >= MAX_ROWS
    assert MAX_CONFIRM_ROWS >= Settings.model_fields["ofx_max_rows"].default


def test_confirm_row_description_matches_the_column():
    assert MAX_DESCRIPTION_LENGTH == 255
    ImportConfirmRequest.model_validate(_body([_row(1, "x" * 255)]))
    with pytest.raises(PydanticValidationError) as exc:
        ImportConfirmRequest.model_validate(_body([_row(1, "x" * 256)]))
    assert exc.value.errors()[0]["loc"] == ("rows", 0, "description")


def test_confirm_still_accepts_an_empty_row_list():
    ImportConfirmRequest.model_validate(_body([]))
