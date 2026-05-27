"""Pydantic schemas for LAI.3 — Smart Budget Rebalance."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class BudgetDeltaSuggestion(BaseModel):
    """One suggested change to an existing budget row.

    ``category_id`` MUST reference an existing master category that
    already has a budget in the current period for the requesting org.
    The service rejects responses whose ``category_id`` set drifts from
    the input budget set (defense-in-depth against prompt injection or
    a misbehaving LLM).
    """

    model_config = ConfigDict(extra="forbid")

    category_id: int
    category_name: str = ""
    current_amount: Decimal = Field(ge=0)
    suggested_amount: Decimal = Field(ge=0)
    delta_amount: Decimal  # may be negative
    reasoning: str = Field(max_length=400)


class BudgetRebalanceResponse(BaseModel):
    """Top-level rebalance response.

    ``status`` is the structural outcome. Frontend renders the modal
    only when ``status == "ok"`` AND ``suggestions`` is non-empty;
    every other status maps to a friendly empty state.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "ok",
        "empty_no_budgets",
        "empty_no_history",
        "llm_unavailable",
    ]
    period_start: Optional[str] = None
    suggestions: list[BudgetDeltaSuggestion] = Field(default_factory=list)
    summary: str = ""
    request_id: Optional[str] = None
