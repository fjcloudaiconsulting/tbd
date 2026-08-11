"""Org membership router (L3.8) — invitations + member management.

Mounted at `/api/v1/orgs`. Admin-gating uses `require_org_admin` from
`app.auth.org_permissions`. Invitation accept/preview are public; the
JWT in the URL is the proof of intent.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.org_permissions import require_org_admin
from app.auth.pat import require_interactive_session
from app.config import settings as app_settings
from sqlalchemy import select

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import Organization, Role, User
from app.rate_limit import get_client_ip, limiter
from app.services import audit_service
from app.routers.auth import _clear_legacy_refresh_cookie, _issue_refresh_session
from app.schemas.auth import TokenResponse
from app.schemas.common import ListEnvelope
from app.schemas.invitation import (
    InvitationAcceptRequest,
    InvitationCreateRequest,
    InvitationPreviewResponse,
    InvitationResponse,
    MemberResponse,
)
from app.security import (
    create_access_token,
    create_invitation_token,
    get_org_session_ttl_seconds,
)
from app.services import invitation_service
from app.services.email_service import send_invitation_email
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


router = APIRouter(prefix="/api/v1/orgs", tags=["org-members"])

logger = structlog.stdlib.get_logger()


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")


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
    dependencies=[Depends(require_interactive_session)],
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


@router.get("/invitations", response_model=ListEnvelope[InvitationResponse])
async def list_invitations(
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Pending invitations for the caller's org as a ``ListEnvelope`` so
    the settings/organization invitations table can sort + page
    server-side. Org-scoped count + page share the same filters."""
    total = await invitation_service.count_pending_invitations(
        db, org_id=current_user.org_id
    )
    try:
        rows = await invitation_service.list_pending_invitations(
            db,
            org_id=current_user.org_id,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail
        ) from exc
    return {
        "items": [_serialize_invitation(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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

    access = create_access_token(user.id, user.org_id, user.role.value)
    # PR 2 (specs/2026-05-17-backend-session-model.md §5.4): write the
    # Redis primary key + family-set entry BEFORE set_cookie. Fails
    # closed (503) on unreachable Redis.
    #
    # Architect P1 finding on PR #306: the Redis write must also come
    # BEFORE ``db.commit()``. ``invitation_service.accept_invitation``
    # flushed (so ``user.id`` is set) but did NOT commit. If Redis is
    # down here, the 503 raises before commit, the open transaction
    # rolls back, and the invitation stays unconsumed — the user can
    # retry. Previous order (commit-then-Redis) consumed the invitation
    # on every Redis blip, permanently locking the invitee out.
    # 2026-05-18 session-stability refactor: invitation accept now
    # respects the inviter org's per-org session TTL setting on the
    # very first cookie issued — same source of truth as login,
    # /refresh, and the Google SSO callback.
    ttl_seconds = await get_org_session_ttl_seconds(db, user.org_id)
    refresh, _jti, _sid = await _issue_refresh_session(
        user.id, ttl_seconds=ttl_seconds
    )
    await db.commit()
    response.set_cookie(
        key="refresh_token",
        value=refresh,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=ttl_seconds,
        # Path=/ so the browser sends the cookie on regular page requests
        # (not just /api/v1/auth/refresh). Required for Next.js RSC to read
        # the cookie via /auth/verify. Mirrors the convention applied in
        # app/routers/auth.py (login, refresh rotation, logout, _issue_tokens,
        # google_callback).
        path="/",
    )
    # Active retirement of any pre-PR #211 ``refresh_token`` cookie at
    # the legacy ``Path=/api/v1/auth/refresh``. See
    # ``app/routers/auth.py:_clear_legacy_refresh_cookie`` for the full
    # rationale and the 2026-05-25 removal target.
    _clear_legacy_refresh_cookie(response)
    return TokenResponse(access_token=access)


@router.get("/members", response_model=ListEnvelope[MemberResponse])
async def list_members(
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Active members of the caller's org as a ``ListEnvelope`` so the
    settings/organization members table can sort + page server-side.
    Org-scoped count + page share the same filters."""
    total = await invitation_service.count_members(db, org_id=current_user.org_id)
    try:
        rows = await invitation_service.list_members(
            db,
            org_id=current_user.org_id,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail
        ) from exc
    return {
        "items": [_serialize_member(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete(
    "/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_interactive_session)],
)
# Bounds audit_events growth now that every refusal writes a row. The cheapest
# trigger needs no target row and no org state — an org admin looping
# DELETE /members/<own_id> hits the self-removal refusal every time — and there
# is no retention/purge job for audit_events. Deliberately generous: 30/minute
# is far beyond any real bulk removal through the UI, so it bounds a loop
# without constraining legitimate use. The audited org-admin sibling
# (orgs.py, org rename) carries 10/hour; removal is more routine, hence looser.
@limiter.limit("30/minute")
async def remove_member(
    user_id: int,
    request: Request,
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # Snapshot the actor's scalars BEFORE the try. The refusal path rolls
    # back, and Session.rollback() expires every loaded instance regardless of
    # expire_on_commit (database.py:89) — current_user is loaded on THIS
    # session (deps.py:52-55), so a later current_user.email read would
    # lazy-load and raise MissingGreenlet, turning the 409 into a 500 and
    # losing the audit row entirely. Pattern: admin_users.py:139-145.
    actor_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id

    try:
        await invitation_service.remove_member(
            db,
            org_id=actor_org_id,
            current_user=current_user,
            target_user_id=user_id,
        )
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    except ConflictError as e:
        # EVERY refusal is audited, not just the superadmin one. Auditing a
        # single branch would make ABSENCE of a row uninterpretable: an
        # operator could not tell "nobody attempted a removal" from "someone
        # attempted one and hit a different guard".
        #
        # Columns, NOT the entity: a Row of plain scalars is a materialized
        # tuple with no identity-map entanglement, so it survives the rollback
        # below. select(User) followed by reading .role after the rollback is a
        # MissingGreenlet. Re-applies the org scope so a refusal audit can
        # never read a foreign-org row. Refusal path only — the successful
        # delete pays zero extra queries. Safe to query here because the
        # service raises before any mutation or flush.
        row = (
            await db.execute(
                select(User.role, User.is_active, User.email, Organization.name)
                .join(Organization, Organization.id == User.org_id)
                .where(User.id == user_id, User.org_id == actor_org_id)
            )
        ).first()
        # Label-addressed, not positional: reordering the columns above must
        # not silently re-map these. `row.role` is a Role enum even on a
        # columns-only SELECT — SQLAlchemy attaches the Enum result processor
        # to the column expression's type, so this behaves identically on
        # MySQL and SQLite. Same shape as the production path at
        # import_service.py:80-88.
        target_role = row.role.value if row else None
        target_is_active = row.is_active if row else None
        target_email = row.email if row else None
        target_org_name = row.name if row else None

        # ⚠ Rollback BEFORE the audit write, not after.
        #
        # Reversing the two does NOT risk a deadlock — the service issued only
        # plain SELECTs, which take no record locks under InnoDB's consistent
        # snapshot reads. The real cost is CONNECTION-POOL AMPLIFICATION: this
        # request would hold its own connection while record_audit_event draws
        # a second, doubling concurrent checkouts against pool_size /
        # max_overflow (database.py). Under a burst of refusals the pool
        # exhausts, record_audit_event raises TimeoutError — and it SWALLOWS
        # it (audit_service.py) — so the audit row vanishes silently, which is
        # the single outcome this change exists to prevent.
        #
        # get_db only closes, never rolls back, so this must be explicit.
        # Pinned by an ordering fence: on SQLite/StaticPool both sessions share
        # one connection, so no behavioural test on the CI backend can see it.
        await db.rollback()

        await logger.awarning(
            "org.member.remove.failed",
            actor_user_id=actor_id,
            target_org_id=actor_org_id,
            target_user_id=user_id,
            reason=e.code,
        )
        # Independent session: the business txn is abandoned, but an attempt
        # against a protected member must be durable regardless
        # (audit_service.py:130-133).
        await audit_service.record_audit_event(
            session_factory,
            event_type="org.member.remove.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=target_org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "target_user_id": user_id,
                "target_email": target_email,
                "target_role": target_role,
                # Distinguishes "tried to lock one out" from "tried to remove
                # an already-locked-out one" — legible only because the
                # superadmin guard sits before the is_active early-return.
                "target_is_active": target_is_active,
                "reason": e.code,
            },
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
