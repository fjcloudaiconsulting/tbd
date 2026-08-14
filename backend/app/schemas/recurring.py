import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RecurringCreate(BaseModel):
    account_id: int
    category_id: int
    description: str
    amount: Decimal = Field(gt=0)
    type: Literal["income", "expense"]
    frequency: Literal["weekly", "biweekly", "monthly", "quarterly", "yearly"]
    next_due_date: datetime.date
    auto_settle: bool = False
    # TBD-275: total instalments this series delivers. Omitted / None =
    # open-ended, which is what every template was before this field existed.
    # ``le`` is a sanity bound, not policy: 1200 monthly instalments is a
    # century, and an unbounded count invites a typo that makes every forecast
    # window walk its full length.
    occurrence_count: Optional[int] = Field(default=None, gt=0, le=1200)


class RecurringUpdate(BaseModel):
    account_id: Optional[int] = None
    category_id: Optional[int] = None
    description: Optional[str] = None
    amount: Optional[Decimal] = Field(default=None, gt=0)
    type: Optional[Literal["income", "expense"]] = None
    frequency: Optional[Literal["weekly", "biweekly", "monthly", "quarterly", "yearly"]] = None
    next_due_date: Optional[datetime.date] = None
    auto_settle: Optional[bool] = None
    is_active: Optional[bool] = None
    # TBD-275. May be edited DOWNWARD below ``occurrences_elapsed``; the series
    # then simply stops (see ``recurring_service.update_recurring``). There is
    # deliberately no ``occurrences_elapsed`` field here -- progress is written
    # by generation only.
    #
    # ⚠ Like every other field on this model, ``None`` means "not supplied" and
    # is indistinguishable from an explicit ``null``, so a counted series
    # cannot be converted back to open-ended through this endpoint. Raising the
    # count is the supported escape.
    occurrence_count: Optional[int] = Field(default=None, gt=0, le=1200)


class RecurringResponse(BaseModel):
    id: int
    account_id: int
    account_name: str = ""
    category_id: int
    category_name: str = ""
    description: str
    amount: Decimal
    type: Literal["income", "expense"]
    frequency: str
    next_due_date: datetime.date
    auto_settle: bool
    is_active: bool
    # TBD-275. Progress is exposed as the two STORED numbers; there is
    # deliberately no ``remaining`` field. A server-computed remainder would be
    # a third representation of one fact, free to disagree with the pair under
    # it, and the client's subtraction cannot. It also keeps the wire contract
    # honest about exhaustion being derived rather than a stored flag.
    occurrence_count: Optional[int] = None
    occurrences_elapsed: int = 0

    model_config = {"from_attributes": True}


class StopRecurringResponse(BaseModel):
    """Result of stopping a template.

    TBD-312: ``demoted_ids`` lists rows that pointed AT a pending row this
    stop deleted, and were marked REJECTED so the FK's ``ON DELETE SET NULL``
    could not turn them back into ordinary, balance-contributing
    transactions. REJECTED is terminal and unreachable through
    ``TransactionUpdate``, so the demotion is irreversible through the API --
    it is reported, never silent, exactly as on
    ``DeleteTransactionResponse``.

    This route previously declared ``response_model=dict``, which validates
    nothing and documents nothing in OpenAPI. Keep ``stopped`` and
    ``pending_removed`` named and typed as they ship: the frontend reads
    ``pending_removed``.
    """

    stopped: bool = True
    pending_removed: int = 0
    demoted_ids: list[int] = Field(default_factory=list)


class DeleteRecurringResponse(BaseModel):
    """Result of deleting a template. See StopRecurringResponse (TBD-312)."""

    deleted: bool = True
    pending_removed: int = 0
    demoted_ids: list[int] = Field(default_factory=list)
