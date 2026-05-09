from typing import Literal, Optional

from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str
    type: Literal["income", "expense", "both"] = "both"
    parent_id: Optional[int] = None
    description: Optional[str] = None


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[Literal["income", "expense", "both"]] = None
    description: Optional[str] = None


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


# ── C0 move / batch-move / delete-with-migration schemas ────────────────────


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
    deleted_rule_count: int = 0
