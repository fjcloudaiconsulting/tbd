import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.budget_rebalance import BudgetDeltaSuggestion


class BudgetDraftResponse(BaseModel):
    """A projection-only draft of budgets for a (next) period.

    ``suggestions`` reuses the rebalance ``BudgetDeltaSuggestion`` shape so
    the frontend can render the same review table, but here every row is a
    NEW budget (``current_amount == 0``): applying the draft CREATES budget
    rows rather than updating existing ones.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "empty_no_history"]
    period_start: Optional[datetime.date] = None
    suggestions: list[BudgetDeltaSuggestion] = Field(default_factory=list)
    summary: str = ""
