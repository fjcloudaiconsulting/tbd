from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.recurring import RecurringCreate, RecurringResponse, RecurringUpdate
from app.services import recurring_service as svc

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


@router.delete("/{recurring_id}", status_code=204)
async def delete_recurring(
    recurring_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await svc.delete_recurring(db, current_user.org_id, recurring_id)


@router.post("/generate", response_model=dict)
async def generate_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await svc.generate_due_transactions(db, current_user.org_id)
    return {"generated": count}
