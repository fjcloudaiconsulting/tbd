"""Schemas for the admin user-management endpoints."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class UserMergeRequest(BaseModel):
    """Body for ``POST /api/v1/admin/users/merge``."""

    source_user_id: int = Field(gt=0, description="row to delete after merge")
    target_user_id: int = Field(gt=0, description="row that survives")


class UserMergeResponse(BaseModel):
    """Per-table count of rows reassigned during the merge."""

    source_user_id: int
    target_user_id: int
    counts: dict[str, int]


class AdminEmailChangeRequest(BaseModel):
    """Body for ``POST /api/v1/admin/users/{user_id}/email-change`` (TBD-362).

    ``new_email_confirm`` is a typed double entry because the operator is
    fixing a typo and a typo in the fix is the obvious failure mode. It is
    compared against ``new_email`` AFTER normalisation, not byte-for-byte: a
    byte comparison rejects a legitimate case difference and trains operators
    to paste both fields, which defeats the confirmation entirely.

    ``reason`` is REQUIRED because there is no user consent anywhere in this
    request, so the forensic note is the only contemporaneous account of why
    it happened.

    ⚠ ``EmailStr`` means a malformed address is a FastAPI **422** raised
    BEFORE the handler runs. There is therefore no handler-side
    ``400 invalid_email``, no audit row for one, and no such code on the
    wire — 422 is unaudited BY CONSTRUCTION, not by omission.
    ``normalize_email`` is ``value.strip().lower()`` and cannot reject
    anything. (``specs/2026-05-22-l4-4-admin-slices.md`` published an
    ``invalid_email`` row; it was wrong there too.)
    """

    new_email: EmailStr
    new_email_confirm: EmailStr
    reason: str = Field(min_length=4, max_length=200)


class AdminEmailChangeResponse(BaseModel):
    """Result of a successful admin-triggered email change.

    ``email_verified`` and ``email`` are echoed back deliberately: they are
    the two columns an operator is most likely to assume this endpoint moved,
    and it moves NEITHER. The response says so in the data rather than only
    in the docs.
    """

    user_id: int
    email: str
    email_verified: bool
    pending_email: str
    previous_pending_email: str | None


class AdminPendingEmailCancelResponse(BaseModel):
    """Result of ``DELETE /api/v1/admin/users/{user_id}/pending-email``.

    ⚠ Carries a body, and the route returns **200**, where the user-side
    sibling (``DELETE /users/me/pending-email``) returns **204**. The
    divergence is deliberate and must not be "harmonised": the operator needs
    to know whether anything was actually cleared, because the state they are
    recovering from is one they cannot otherwise observe.
    """

    cleared: bool
