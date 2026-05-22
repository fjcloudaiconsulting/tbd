"""Schemas for LAI.1 — AI-assisted transaction categorization.

The endpoint accepts an existing transaction id (org-scoped) and
returns a suggested category id plus the model's confidence and a
short reasoning string. Suggestions are advisory: the frontend
prefills the category dropdown but never auto-applies. The user
must accept the suggestion explicitly.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CategorizeRequest(BaseModel):
    """POST /api/v1/ai/categorize body.

    A single transaction id is enough: the backend fetches the row,
    confirms org ownership, builds the prompt from server-side state,
    and submits to the LLM. Sending description/amount from the client
    would let an adversarial caller fuzz around the org's catalog.
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: int = Field(gt=0)


class CategorizeSuggestion(BaseModel):
    """Successful suggestion payload."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: int
    category_id: int
    category_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)
