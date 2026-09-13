from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.recurring import (
    DeleteRecurringResponse,
    OccurrenceRequest,
    RecurringCreate,
    RecurringResponse,
    RecurringUpdate,
    StopRecurringResponse,
)
from app.schemas.transaction import TransactionResponse
from app.services import recurring_service as svc
from app.services.transaction_service import to_response as tx_to_response

router = APIRouter(prefix="/api/v1/recurring", tags=["recurring"])


@router.get("", response_model=list[RecurringResponse])
async def list_recurring(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await svc.list_recurring(db, current_user.org_id)
    return [svc.to_response(r) for r in items]


@router.post("", response_model=RecurringResponse, status_code=201)
async def create_recurring(
    body: RecurringCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await svc.create_recurring(db, current_user.org_id, body)
    return svc.to_response(r)


@router.put("/{recurring_id}", response_model=RecurringResponse)
async def update_recurring(
    recurring_id: int,
    body: RecurringUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await svc.update_recurring(db, current_user.org_id, recurring_id, body)
    return svc.to_response(r)


@router.post("/{recurring_id}/stop", response_model=StopRecurringResponse)
async def stop_recurring(
    recurring_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    outcome = await svc.stop_recurring(db, current_user.org_id, recurring_id)
    return StopRecurringResponse(
        pending_removed=outcome.removed, demoted_ids=outcome.demoted_ids
    )


@router.delete("/{recurring_id}", response_model=DeleteRecurringResponse)
async def delete_recurring(
    recurring_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    outcome = await svc.delete_recurring(db, current_user.org_id, recurring_id)
    return DeleteRecurringResponse(
        pending_removed=outcome.removed, demoted_ids=outcome.demoted_ids
    )


@router.post(
    "/{recurring_id}/skip-next", response_model=TransactionResponse, status_code=201
)
async def skip_next(
    recurring_id: int,
    body: OccurrenceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Skip the next occurrence (TBD-272): writes it as a skipped PENDING row
    at the frontier and spends it. Terminal."""
    tx = await svc.materialise_next(
        db, current_user.org_id, recurring_id,
        occurrence_date=body.occurrence_date, skipped=True,
    )
    return tx_to_response(tx)


@router.post(
    "/{recurring_id}/materialise-next", response_model=TransactionResponse, status_code=201
)
async def materialise_next(
    recurring_id: int,
    body: OccurrenceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write the next occurrence now (TBD-273, "edit next"). Its amount is then
    edited with ``PUT /transactions/{id}``; the template is untouched."""
    tx = await svc.materialise_next(
        db, current_user.org_id, recurring_id,
        occurrence_date=body.occurrence_date, skipped=False,
    )
    return tx_to_response(tx)


@router.post("/generate", response_model=dict)
async def generate_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.generate_due_transactions(db, current_user.org_id)
