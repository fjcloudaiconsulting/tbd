from typing import Literal, Optional

from pydantic import BaseModel


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
