"""Org membership router (L3.8) — invitations + member management.

Mounted at `/api/v1/orgs`. Admin-gating uses `require_org_admin` from
`app.auth.org_permissions`. Invitation accept/preview are public; the
JWT in the URL is the proof of intent.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.org_permissions import require_org_admin
from app.config import settings as app_settings
from sqlalchemy import select

from app.database import get_db
from app.deps import get_current_user
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.schemas.auth import TokenResponse
from app.schemas.invitation import (
    InvitationAcceptRequest,
    InvitationCreateRequest,
    InvitationPreviewResponse,
    InvitationResponse,
    MemberResponse,
)
from app.security import create_access_token, create_invitation_token, create_refresh_token
from app.services import invitation_service
from app.services.email_service import send_invitation_email
from app.services.exceptions import ConflictError, NotFoundError


router = APIRouter(prefix="/api/v1/orgs", tags=["org-members"])


def _serialize_invitation(inv) -> InvitationResponse:
    return InvitationResponse(
        id=inv.id,
        email=inv.email,
        role=inv.role.value,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        inviter_username=getattr(inv.inviter, "username", None) if inv.__dict__.get("inviter") else None,
        status="pending",
    )


def _serialize_member(u: User) -> MemberResponse:
    return MemberResponse(
        id=u.id, username=u.username, email=u.email,
        role=u.role.value, is_active=u.is_active,
    )


def _invitation_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"code": "invitation_unavailable", "message": "This invitation is no longer available."},
    )


@router.post(
    "/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    body: InvitationCreateRequest,
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        inv = await invitation_service.create_invitation(
            db,
            org_id=current_user.org_id,
            created_by=current_user.id,
            email=body.email,
            role=Role(body.role),
        )
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    # Capture serializable fields BEFORE commit — once the session
    # expires the instance, attribute access would trigger a lazy
    # reload and trip MissingGreenlet on the prod async engine.
    snapshot = InvitationResponse(
        id=inv.id,
        email=inv.email,
        role=inv.role.value,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        inviter_username=current_user.username,
        status="pending",
    )
    token = create_invitation_token(inv.id, inv.email)
    accept_url = f"{app_settings.app_url}/accept-invite?token={token}"
    inviter_name = (
        " ".join(filter(None, [current_user.first_name, current_user.last_name]))
        or current_user.username
    )
    org = (
        await db.execute(
            select(Organization).where(Organization.id == current_user.org_id)
        )
    ).scalar_one()
    await db.commit()
    # Email send happens after commit so a Mailgun outage doesn't roll
    # back the invite (admin can revoke and re-invite).
    try:
        await send_invitation_email(
            body.email, inviter_name=inviter_name, org_name=org.name, accept_url=accept_url,
        )
    except Exception:
        # Logged inside email_service. Don't fail the API call — the
        # row exists and admin can revoke + re-invite.
        pass
    return snapshot


@router.get("/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await invitation_service.list_pending_invitations(
        db, org_id=current_user.org_id,
    )
    return [_serialize_invitation(r) for r in rows]


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invitation(
    invitation_id: int,
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await invitation_service.revoke_invitation(
            db, org_id=current_user.org_id, invitation_id=invitation_id,
        )
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/invitations/preview",
    response_model=InvitationPreviewResponse,
)
@limiter.limit("30/minute")
async def preview_invitation(
    request: Request,
    token: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await invitation_service.preview_invitation(db, token=token)
    except invitation_service.InvitationUnavailable:
        raise _invitation_unavailable()


@router.post("/invitations/accept", response_model=TokenResponse)
@limiter.limit("10/minute")
async def accept_invitation(
    request: Request, payload: InvitationAcceptRequest, response: Response,
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await invitation_service.accept_invitation(
            db, token=payload.token, username=payload.username, password=payload.password,
        )
    except invitation_service.InvitationUnavailable:
        raise _invitation_unavailable()
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    await db.commit()

    access = create_access_token(user.id, user.org_id, user.role.value)
    refresh = create_refresh_token(user.id)
    response.set_cookie(
        key="refresh_token",
        value=refresh,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/api/v1/auth/refresh",
    )
    return TokenResponse(access_token=access)


@router.get("/members", response_model=list[MemberResponse])
async def list_members(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await invitation_service.list_members(db, org_id=current_user.org_id)
    return [_serialize_member(r) for r in rows]


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: int,
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await invitation_service.remove_member(
            db,
            org_id=current_user.org_id,
            current_user=current_user,
            target_user_id=user_id,
        )
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
