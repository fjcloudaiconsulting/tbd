from typing import Literal, Optional

from pydantic import BaseModel, Field


# BE-27: the ``categories.name`` / ``.description`` column lengths, so an
# over-long value is a 422 rather than a database error.
CATEGORY_NAME_MAX_LENGTH = 100
CATEGORY_DESCRIPTION_MAX_LENGTH = 255


class CategoryCreate(BaseModel):
    name: str = Field(max_length=CATEGORY_NAME_MAX_LENGTH)
    type: Literal["income", "expense", "both"] = "both"
    parent_id: Optional[int] = None
    description: Optional[str] = Field(default=None, max_length=CATEGORY_DESCRIPTION_MAX_LENGTH)


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=CATEGORY_NAME_MAX_LENGTH)
    type: Optional[Literal["income", "expense", "both"]] = None
    description: Optional[str] = Field(default=None, max_length=CATEGORY_DESCRIPTION_MAX_LENGTH)


class CategoryResponse(BaseModel):
    id: int
    name: str
    type: Literal["income", "expense", "both"]
    parent_id: Optional[int] = None
    parent_name: Optional[str] = None
    description: Optional[str] = None
    slug: Optional[str] = None
    is_system: bool = False
    transaction_count: int = 0

    model_config = {"from_attributes": True}


# --- C0 move / batch-move / delete-with-migration schemas ------------------


class CategoryMoveRequest(BaseModel):
    target_parent_id: int


class BatchMoveItem(BaseModel):
    subcategory_id: int
    target_parent_id: int


class BatchMoveRequest(BaseModel):
    moves: list[BatchMoveItem] = Field(..., min_length=1)


class CategoryMoveResult(BaseModel):
    category_id: int
    source_master_id: int
    target_master_id: int
    affected_transaction_count: int
    affected_recurring_count: int
    affected_forecast_item_count: int
    budget_actuals_shifted: bool


class BatchMoveResult(BaseModel):
    moves: list[CategoryMoveResult]


class CategoryDeleteResult(BaseModel):
    deleted_category_id: int
    migration_target_id: Optional[int] = None
    migrated_transaction_count: int = 0
    migrated_recurring_count: int = 0
    migrated_forecast_item_count: int = 0
    migrated_rule_count: int = 0
    # Source rules dropped as duplicates of a rule the target already owned.
    deleted_rule_count: int = 0


class RestoreRecommendedResult(BaseModel):
    """Result of POST /api/v1/categories/restore-recommended.

    ``created_count`` is the number of brand-new system-category rows
    inserted; existing slugs were left untouched (idempotent).
    Category Fallback design Layer C (post-L3.10).
    """

    created_count: int
