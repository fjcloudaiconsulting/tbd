"""Pydantic schemas for description-suggestion autocomplete (Wave 1 contract).

Frozen per spec at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.

This endpoint powers the manual-entry form's "description" field
autocomplete. It returns the user's most-used descriptions for a given
transaction type, ranked by prefix-match → frequency → recency.

Privacy rules (server-enforced):
- Source: user's own ``transactions`` table, filtered by ``org_id``.
- Never crosses org boundaries (single SQL filter).
- Never returns suggestions from ``merchant_dictionary`` (that's smart-rules
  categorization infrastructure, not autocomplete).
- Never logs raw descriptions or the raw ``q`` query string — only
  ``org_id``, ``type``, ``query_length``, ``result_count``.

Frontend behavior expectations:
- Debounce 300 ms.
- Min query length 2 chars when ``q`` is present.
- When ``q`` is omitted, server returns top-N most-used descriptions for
  the requested ``type`` (useful for the manual-entry form's "recent
  descriptions" hint).
- Show max 8 items in the dropdown.

Wave 2 Description Suggestions team owns the implementation. This file
freezes the request/response wire shape.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DescriptionSuggestionsRequest(BaseModel):
    """Query parameters for the suggestion endpoint.

    Included for OpenAPI documentation; the actual route uses
    ``Query()`` parameters on the function signature.

    Fields:
        type: Transaction type filter. ``transfer`` is included for
            future use but is not currently surfaced in the manual-entry
            UX (transfers go through their own creation flow).
        q: Search query. Optional. When present, minimum 2 chars
            (server returns 422 on shorter queries). When absent,
            returns the top-N most-used descriptions for ``type``.
        limit: Maximum number of results. Default 10, max 25.
    """

    type: Literal["income", "expense", "transfer"]
    q: str | None = Field(default=None, min_length=2, max_length=255)
    limit: int = Field(default=10, ge=1, le=25)

    model_config = ConfigDict(extra="forbid")


class DescriptionSuggestion(BaseModel):
    """A single autocomplete suggestion.

    Fields:
        description: The exact text the user previously used. Returned
            verbatim (case-preserved).
        category_id: The most-frequently-paired category for this
            description, scoped to the org. When the same description
            has been used with multiple categories, the most-used wins
            (ties broken by recency).
        category_name: Display name of ``category_id`` at query time.
        use_count: Number of times this user/org has used this description.
        last_used: Date of the most recent transaction with this
            description.
    """

    description: str
    category_id: int
    category_name: str
    use_count: int = Field(ge=1)
    last_used: datetime.date

    model_config = ConfigDict(extra="forbid")


class DescriptionSuggestionsResponse(BaseModel):
    """Response body for ``GET /api/v1/transactions/suggestions/descriptions``.

    Sorted by:
      1. Prefix match first (description ILIKE q || '%').
      2. Then frequency (use_count DESC).
      3. Then recency (last_used DESC).

    Fields:
        suggestions: Ranked list, max ``limit`` entries.
    """

    suggestions: list[DescriptionSuggestion] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
