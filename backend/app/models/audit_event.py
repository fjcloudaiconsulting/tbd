"""Durable audit log for superadmin platform actions (L4.7).

The structlog ``admin.org.*`` and ``org.data.*`` events emitted by the
admin and tenant routers stream to stdout (and from there to whatever
log sink ops wires up). They're great for triage but they're not a
queryable history with SLA-grade retention. This table persists the
same events into a durable, indexable store so superadmins can answer
"who did what to whom, and when" from the admin UI without grepping
container logs.

Two design choices worth restating in code:

1. **Independent-session writes.** The recording function opens its
   own ``AsyncSession`` from the engine-wide factory, commits, and
   swallows exceptions after logging. An audit-write failure must
   never poison the business transaction it describes — and a
   business rollback (e.g. ``admin.org.delete.failed``) must still
   produce an audit row, which only works if the audit write isn't
   inside the rolled-back txn.

2. **Survives org wipe.** ``audit_events.target_org_id`` uses
   ``ON DELETE SET NULL`` so deleting an organization (or wiping
   its data via the tenant reset path) leaves the audit history
   intact. The ``target_org_name`` snapshot column preserves the
   org name at the moment of the event, which is the only sane
   thing to display in the UI after the org is gone. Same trick for
   ``actor_user_id`` / ``actor_email``.

3. **``api_token_id`` is the ACTOR credential, never the subject**
   (TBD-188). It means: *the API token presented as the credential
   for the request that produced this row.* On ``outcome="success"``
   rows it additionally validated; on ``api_token.auth_rejected``
   rows it was presented and rejected. It is resolved from the
   request-scoped structlog contextvar inside
   ``audit_service._build_audit_event`` — never passed by a caller —
   so a new audit call site is attributed by construction.

   ⚠ ``api_token.created`` / ``api_token.revoked`` rows MUST keep
   this column NULL. Those routes are ``require_interactive_session``
   so the *actor* is a JWT session; the token they name is the
   *subject* and stays in ``detail["api_token_id"]``. Copying the
   subject into this column (the obvious-looking "fix") permanently
   merges two different facts into one field and makes "everything
   token 42 did" return the row where a human revoked token 42.

   **Where NULL comes from.** No API token was resolved as the
   credential: an interactive JWT session, a pre-auth or anonymous
   route, or a scheduler task (whose lifespan-spawned context
   snapshot is empty). Plus one documented gap — a ``pat_`` bearer
   presented to an OPTIONAL-auth route is silently anonymous:
   ``deps.get_current_user_optional`` decodes the credential as a
   JWT and has no ``pat_`` branch, so ``authenticate_pat`` never
   runs and nothing binds. That row is NULL even though a token
   *was* presented as the credential, which is literally what this
   column says it means. Harmless today — the only optional-auth
   route is ``GET /api/v1/auth/status``, which writes no audit rows
   — and real the moment an optional-auth route starts auditing.

   ⚠ **``api_tokens`` rows are never hard-deleted, and this column's
   ``ON DELETE SET NULL`` is only free because of that.** The
   cheapness argument holds for ``audit_service.record_audit_event``,
   which writes on an independent session and swallows (worst case:
   one lost audit row). It does NOT hold for
   ``audit_service.add_audit_event_to_session``, which stages the row
   on the CALLER's session and deliberately does not swallow, so an
   FK violation surfaces at the business commit. Of its eleven call
   sites, nine are PAT-reachable (``routers/categories.py`` ×4,
   ``services/category_service.py`` ×4, and
   ``services/transaction_service.py::adjust_account_balance``); the
   other two — ``routers/orgs.py`` rename and ``routers/admin_orgs.py``
   delete — sit under ``require_interactive_session`` and can never be
   PAT-authed. If a token row were hard-deleted while one of those
   nine were in flight, the staged audit row's FK would violate and
   roll back the *user's business write* with a 500 — not merely lose
   an audit row. Revocation is a ``revoked_at`` stamp precisely so
   this is unreachable; anything that introduces a real ``DELETE`` on
   ``api_tokens`` must revisit this FK first.

L4.4 admin-slices event-type taxonomy (seeded 2026-05-22, spec
``specs/2026-05-22-l4-4-admin-slices.md`` §8). These strings are
the durable contract every L4.4 router commits to emit; the
implementation lives in PRs 2-5 of the train. Listed here so a
``grep`` for the event-type string lands on a stable definition
even before the emitting code arrives.

* ``admin.platform_admin.invitation.sent`` — superadmin issues a
  platform-admin invitation. actor=superadmin, target_org_id=NULL.
* ``admin.platform_admin.invitation.revoked`` — pending invite
  cancelled. actor=superadmin, target_org_id=NULL.
* ``admin.platform_admin.invitation.accepted`` — invitee creates
  their is_superadmin=True user. actor=new user (self-target),
  target_org_id=new org's id.
* ``admin.user.password_reset.triggered`` — admin-triggered
  out-of-band password reset email. actor=superadmin,
  target_org_id=target.org_id.
* ``admin.user.email_change.triggered`` — admin-triggered email
  change with two-key typed confirmation. actor=superadmin,
  target_org_id=target.org_id. Note: when the target user later
  confirms via verification link, an additional ``user.email.changed``
  row is written (existing user-initiated convention).

  ⚠ SHIPPED 2026-08-24 (TBD-362), AND THE MECHANISM IS NOT THE ONE
  THE PARAGRAPH ABOVE ORIGINALLY IMPLIED. This seed predates TBD-361
  and was written against a SINGLE-PHASE change in which the endpoint
  moved the address itself. It does not. The operator writes
  ``users.pending_email`` and NOTHING else — not ``users.email``, not
  ``email_verified``, not ``sessions_invalidated_at`` — and the
  account stays locked out until the user proves control of the new
  address by clicking. (The ``user.email.changed`` sentence above
  survives unchanged and is still correct: that row is written by
  ``auth.verify_email``'s promoting branch when they click.)

  ``target_org_id`` is read off the TARGET and must be set:
  ``/admin/audit``'s only org filter is that column, so a NULL row is
  invisible to every org-scoped query. ⚠ ``actor_email`` is the
  SUPERADMIN's, diverging from the self-initiated convention —
  ``audit_events`` has no ``target_user_id`` column, so it is the only
  identity column, and on an admin-triggered row the reader's question
  is "which operator"; the target's old address survives in
  ``detail``. ``detail`` carries ``target_user_id``,
  ``target_email_old`` (promotion overwrites ``users.email`` and
  nothing else preserves the typo), ``target_pending_email``,
  ``previous_pending_email`` (this write IS the "overwrite by a later
  request" clearer, so without it a destroyed claim leaves no trace),
  ``reason``, and ``kind``.
* ``admin.user.email_change.cancelled`` — an operator cleared a
  pending claim via ``DELETE /api/v1/admin/users/{id}/pending-email``
  (TBD-362). actor=superadmin, target_org_id=target.org_id,
  ``outcome="success"``. ``detail`` carries ``target_user_id`` and
  ``previous_pending_email``, which is the ONLY record of the
  destroyed claim — the column is NULL after this write. Emitted only
  when a claim was actually cleared: the idempotent no-op is not a
  transition and records nothing.
* ``admin.user.email_change.failed`` — every refusal of
  ``POST /api/v1/admin/users/{id}/email-change`` (TBD-362), written on
  the independent session AFTER the rollback, ``outcome="failure"``
  always. ⚠ Includes the **404**, diverging from ``delete_user``,
  which deliberately does not audit its 404: probing THIS path carries
  an attacker-supplied DESTINATION address, where the delete path
  carries no body at all. ``detail`` carries the PRE-REFUSAL snapshot
  (``target_email_verified``, ``target_is_active``,
  ``target_is_superadmin``) alongside ``code``, ``attempted_email`` and
  ``reason``, because a row saying only "409" cannot distinguish "this
  account was already locked out" from "an operator just attacked an
  active superadmin".
* ``admin.user.mfa_disabled`` — admin clears mfa_enabled +
  totp_secret + recovery_codes server-side; user re-enrols on next
  login. actor=superadmin, target_org_id=target.org_id. REQUIRED
  ``detail.reason`` (free text, max 200).
* ``admin.impersonation.entered`` — read-only impersonation session
  starts; 15-min Redis-backed jti. actor=superadmin,
  target_org_id=target.org_id.
* ``admin.impersonation.exited`` — read-only impersonation session
  ends (manual exit or natural expiry). actor=superadmin,
  target_org_id=target.org_id. ``detail.ended_by`` distinguishes
  ``manual`` from ``expiry``.
* ``admin.impersonation.revoked`` — impersonation session force-
  ended because the actor lost ``is_superadmin`` mid-session (Q6
  lock, §5.7). actor=ex-superadmin, target_org_id from session
  blob. ``detail.reason="actor_superadmin_revoked"``.

The existing ``org.invitation.sent`` / ``org.invitation.accepted``
event types gain a ``detail.via_platform_admin: true`` flag when
issued by a superadmin acting on an org (no new event_type — the
flag rides on the existing row). Implementation in PR 2.

⚠ Neither of those two strings is emitted anywhere in ``app/`` today.
This paragraph is a forward contract, not a description of shipped
behaviour, and ``tests/models/test_audit_event_taxonomy.py`` asserts
only that they appear *in this docstring*. Tracked in TBD-376.

Tenant org-membership types (TBD-364):

* ``org.member.remove.failed`` — ``DELETE /api/v1/orgs/members/{id}``
  was refused. actor=the org ADMIN/OWNER who attempted it,
  target_org_id=their org, ``outcome="failure"`` always. **Every**
  refusal reason is emitted, not only the protected-target one, so
  that ABSENCE of a row is interpretable — one-of-N coverage would
  leave an operator unable to distinguish "nobody attempted a
  removal" from "someone attempted one and hit a different guard".
  ``detail.reason`` carries the ``invitation_service.CODE_*`` value
  (``self_removal``, ``target_is_platform_superadmin``,
  ``owner_removal_requires_owner``, ``last_active_owner``), alongside
  ``target_user_id``, ``target_email``, ``target_role`` and
  ``target_is_active``. The last of those distinguishes an attempt
  against an already-locked-out account from a fresh lockout.
  404s are deliberately not audited (different HTTP class; auditing
  them opens the member-enumeration-probe question).

  ⚠ The success counterpart ``org.member.removed`` is deliberately
  NOT emitted yet — the name is reserved for TBD-375, which must
  first rule on notification parity. Do not squat it.

User-initiated email-change types (TBD-361 / TBD-362):

* ``user.email.change_requested`` — the user recorded a CLAIM on a new
  address via ``PUT /api/v1/users/me``. Nothing about their identity
  has changed; ``users.email`` still holds the old address.
  actor=self, ``actor_email`` = the OLD address so a "who was this"
  lookup after a malicious swap can recover it.
* ``user.email.changed`` — the claim was PROVEN and promoted onto
  ``users.email`` by ``auth.verify_email``'s promoting branch. This is
  the row that says it happened, written where it happened.
* ``user.email.change_cancelled`` — the user abandoned a live claim
  via ``DELETE /api/v1/users/me/pending-email`` (added TBD-362; that
  route wrote no audit row at all before). actor=self,
  target_org_id=their org, ``outcome="success"``.
  ``detail.cancelled_pending_email`` carries the destroyed address,
  which nothing else preserves. Emitted only when a claim was actually
  cleared — the idempotent no-op records nothing.

  ⚠ The route's ABSENCE OF RE-AUTH is deliberate and correct, and was
  deliberately left alone when this row was added. Requesting a change
  moves the account's recovery channel and demands proof of presence;
  cancelling one only restores the status quo and can move nothing.
  Demanding a password to undo a mistake is the exact shape that made
  the original TBD-361 defect unrecoverable.

⚠ ``tests/models/test_audit_event_taxonomy.py`` is ONE-DIRECTIONAL: it
asserts that documented strings appear in this docstring, and nothing
fences a NEWLY EMITTED string that was never documented. Every new
event type therefore has to be added here by hand, in the same PR that
starts emitting it, or it ships undocumented and unfenced.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditOutcome(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class AuditEvent(Base):
    __tablename__ = "audit_events"

    # BigInteger on MySQL (audit logs grow forever and we don't want
    # to wedge against the 32-bit ceiling), but SQLite's autoincrement
    # only honours INTEGER (not BIGINT) — `with_variant` keeps the
    # in-memory test path on a real autoincrementing column.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(80), nullable=False, index=True
    )
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Snapshot — the actor's email at event time, never resolved
    # through the FK (which can be NULL after user deletion).
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    target_org_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Snapshot — same rationale as actor_email.
    target_org_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    # The API token PRESENTED AS THE CREDENTIAL for this request (TBD-188).
    # See design note 3 in the module docstring — actor, never subject.
    # BigInteger mirrors ``api_tokens.id``; MySQL rejects an FK whose
    # referencing column type differs from the referenced PK, so a plain
    # ``Integer`` here would pass SQLite CI and fail at ALTER TABLE on prod.
    api_token_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("api_tokens.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    outcome: Mapped[AuditOutcome] = mapped_column(
        Enum(AuditOutcome, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(6),
        nullable=False,
        index=True,
    )
