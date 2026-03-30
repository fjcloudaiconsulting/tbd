from typing import Literal, Optional

from pydantic import BaseModel


class CategoryCreate(BaseModel):
    name: str
    type: Literal["income", "expense", "both"] = "both"


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[Literal["income", "expense", "both"]] = None


class CategoryResponse(BaseModel):
    id: int
    name: str
    type: str
    transaction_count: int = 0

    model_config = {"from_attributes": True}
