"""Pydantic schema contract tests for the transfer-related write schemas."""
import pytest
from pydantic import ValidationError

from app.schemas.transaction import (
    ConvertToTransferRequest,
    TransactionPairRequest,
    UnpairTransactionRequest,
)


def test_pair_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        TransactionPairRequest(expense_id=1, income_id=2, surprise="x")


def test_pair_request_defaults():
    body = TransactionPairRequest(expense_id=1, income_id=2)
    assert body.transfer_category_id is None
    assert body.recategorize is True


def test_convert_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ConvertToTransferRequest(destination_account_id=10, surprise="x")


def test_convert_request_pair_with_optional():
    body = ConvertToTransferRequest(destination_account_id=10)
    assert body.pair_with_transaction_id is None


def test_unpair_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        UnpairTransactionRequest(
            expense_fallback_category_id=1,
            income_fallback_category_id=2,
            surprise="x",
        )


def test_unpair_request_requires_both_fallbacks():
    with pytest.raises(ValidationError):
        UnpairTransactionRequest(expense_fallback_category_id=1)


def test_transaction_update_accepts_settled_date():
    """settled_date is now a settable field on TransactionUpdate."""
    from app.schemas.transaction import TransactionUpdate
    import datetime
    body = TransactionUpdate(settled_date=datetime.date(2026, 5, 4))
    assert body.settled_date == datetime.date(2026, 5, 4)


def test_transaction_update_settled_date_optional():
    """settled_date defaults to None."""
    from app.schemas.transaction import TransactionUpdate
    body = TransactionUpdate(description="x")
    assert body.settled_date is None


def test_transaction_create_accepts_settled_date_for_pending():
    """TransactionCreate must accept settled_date so callers can stamp an
    expected settlement date on pending rows at creation time (Item 13).
    """
    from app.schemas.transaction import TransactionCreate
    import datetime
    from decimal import Decimal
    body = TransactionCreate(
        account_id=1, category_id=1,
        description="cc", amount=Decimal("1.00"),
        type="expense", status="pending",
        date=datetime.date(2026, 5, 1),
        settled_date=datetime.date(2026, 6, 1),
    )
    assert body.settled_date == datetime.date(2026, 6, 1)


def test_transaction_create_settled_date_optional():
    """settled_date defaults to None — settled rows fall back to date in
    the service layer, pending rows just don't have an expected date.
    """
    from app.schemas.transaction import TransactionCreate
    import datetime
    from decimal import Decimal
    body = TransactionCreate(
        account_id=1, category_id=1,
        description="x", amount=Decimal("1.00"),
        type="expense", status="settled",
        date=datetime.date(2026, 5, 1),
    )
    assert body.settled_date is None


def test_transaction_create_rejects_settled_date_before_date():
    """Cross-field validator: settled_date < date is invalid at the schema."""
    from app.schemas.transaction import TransactionCreate
    from pydantic import ValidationError
    import datetime
    from decimal import Decimal
    import pytest as _pytest
    with _pytest.raises(ValidationError):
        TransactionCreate(
            account_id=1, category_id=1,
            description="x", amount=Decimal("1.00"),
            type="expense", status="pending",
            date=datetime.date(2026, 5, 10),
            settled_date=datetime.date(2026, 5, 1),
        )
