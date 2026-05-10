"""Tag pydantic schemas (PR-Tags-A).

Public API surface mirrors ``app/schemas/category.py`` style: thin
request/response models, Decimal/date types pass through, no DB-layer
concerns. The suggestion endpoint's response shape is the most
load-bearing piece: the frontend autocomplete reads ``source`` to
decide whether to render a subtle differentiator.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Per-tag name length matches the model column. Keeping the constant in
# one place avoids drift between schema and DB.
TAG_NAME_MAX_LENGTH = 32

# Max tags attachable to a single transaction. Spec section 2.5 pins 5.
MAX_TAGS_PER_TRANSACTION = 5

# K-anonymity floor for cross-org dictionary suggestion. A non-seed
# dictionary entry only surfaces in the shared_dictionary suggestion
# pass once at least this many distinct orgs have contributed it. Spec
# section 3.2.
SHARED_DICTIONARY_MIN_CONTRIBUTORS = 3


class TagCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=TAG_NAME_MAX_LENGTH)


class TagRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=TAG_NAME_MAX_LENGTH)


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    name_normalized: str
    usage_count: int = 0


class TransactionTagSetReplace(BaseModel):
    """PUT body for replacing the full tag set on a transaction.

    The cap is enforced at both the schema layer (``max_length=5``) and
    the service layer (defense in depth). Submitting more than 5 returns
    422 from FastAPI before the request reaches the service.
    """
    model_config = ConfigDict(extra="forbid")

    tag_names: list[str] = Field(
        default_factory=list,
        max_length=MAX_TAGS_PER_TRANSACTION,
    )


# Suggestion source enum. Strict literal so a typo on the service side
# would surface at type-check time. Order is the precedence order.
SuggestionSource = Literal["org_co_category", "org_recent", "shared_dictionary"]


class TagSuggestion(BaseModel):
    name: str
    source: SuggestionSource
    weight: int


class TagSuggestionsResponse(BaseModel):
    suggestions: list[TagSuggestion]


# ---- Optional embed for transaction responses (added in PR-Tags-A as
# part of the API contract; the transactions router will populate this
# only when the caller has selectinload'd the join). Service layer wires
# this; schemas just expose the shape.
class _TagInTransactionResponse(BaseModel):
    """Embedded tag shape on transaction list/detail responses."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


__all__ = [
    "MAX_TAGS_PER_TRANSACTION",
    "SHARED_DICTIONARY_MIN_CONTRIBUTORS",
    "TAG_NAME_MAX_LENGTH",
    "TagCreate",
    "TagRename",
    "TagResponse",
    "TagSuggestion",
    "TagSuggestionsResponse",
    "TransactionTagSetReplace",
    "SuggestionSource",
]
