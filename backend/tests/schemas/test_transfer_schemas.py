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
