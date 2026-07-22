"""Credit Card field validation service (Credit Card Model V1, Slice 1).

Single-purpose validator for the four CC-only columns on ``accounts``
(``specs/2026-07-22-cc-model-v1-design.md`` § Validation). Mirrors the
``payment_source_service`` convention: plain sync helper, raises
``HTTPException(422)`` on violation, returns ``None`` on success. Called
by the accounts router from both the create path and the shared
``_apply_non_type_fields`` update path against the resulting row state.

Rules:
  - Non-CC target: all four CC-only columns MUST be NULL.
  - CC target:
      * credit_limit optional; if set must be > 0 (non-enforcing: no
        balance <= limit check anywhere).
      * apr optional; if set must be in [0, 100] (stored as a percent).
      * fixed_payment_amount required and > 0 iff payment_strategy ==
        fixed_amount; forbidden otherwise.

Deliberate status divergence: these rules use 422 (matching
payment_source_service); the older close_day rules use 400. Accepted.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

from fastapi import HTTPException

from app.models.account import PaymentStrategy


_CC = "credit_card"
_APR_LO = Decimal("0")
_APR_HI = Decimal("100")


def validate_credit_card_fields(
    *,
    target_slug: Optional[str],
    credit_limit: Optional[Decimal],
    apr: Optional[Decimal],
    payment_strategy: Optional[Union[PaymentStrategy, str]],
    fixed_payment_amount: Optional[Decimal],
) -> None:
    """Validate the four CC-only field values against the target slug.

    Raises ``HTTPException(422)`` on any violation; returns ``None`` on
    success. ``payment_strategy`` may be a ``PaymentStrategy`` enum or its
    raw string value; both are normalized.
    """
    strategy = (
        payment_strategy.value
        if isinstance(payment_strategy, PaymentStrategy)
        else payment_strategy
    )

    if target_slug != _CC:
        if credit_limit is not None:
            raise HTTPException(
                status_code=422,
                detail="credit_limit is only allowed on credit_card accounts",
            )
        if apr is not None:
            raise HTTPException(
                status_code=422,
                detail="apr is only allowed on credit_card accounts",
            )
        if strategy is not None:
            raise HTTPException(
                status_code=422,
                detail="payment_strategy is only allowed on credit_card accounts",
            )
        if fixed_payment_amount is not None:
            raise HTTPException(
                status_code=422,
                detail="fixed_payment_amount is only allowed on credit_card accounts",
            )
        return

    # CC target.
    if credit_limit is not None and credit_limit <= 0:
        raise HTTPException(
            status_code=422,
            detail="credit_limit must be greater than 0",
        )
    if apr is not None and not (_APR_LO <= apr <= _APR_HI):
        raise HTTPException(
            status_code=422,
            detail="apr must be between 0 and 100",
        )

    if strategy == PaymentStrategy.FIXED_AMOUNT.value:
        if fixed_payment_amount is None or fixed_payment_amount <= 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "fixed_payment_amount is required and must be greater "
                    "than 0 for the fixed_amount payment strategy"
                ),
            )
    else:
        if fixed_payment_amount is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "fixed_payment_amount is only allowed with the "
                    "fixed_amount payment strategy"
                ),
            )
