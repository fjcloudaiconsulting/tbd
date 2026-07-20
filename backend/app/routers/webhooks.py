"""Public Mailgun delivery-webhook sink.

Mailgun POSTs one signed delivery event per request here
(``delivered`` / ``permanent_fail`` / ``temporary_fail`` / ``complained``).
The endpoint is **public** (zero auth dependency) by design: Mailgun sends
these events without any of our auth context, exactly like the CSP report
sink. Security rests on the HMAC signature in the JSON body, verified
before any DB work is done (W1/W3).

See ``specs/2026-07-20-mailgun-delivery-webhooks-design.md``, rulings
W2/W3/W5/W6/W7/W10/W11. Load-bearing points:

* **Processing order is cheap→expensive, DB LAST** (W3): content-length
  precheck → capped body read → JSON parse → signature verify → replay
  dedup → event map → correlation + conditional update.
* **Status discipline (W2):** 404 = signing key unset (fail closed); 401 =
  bad / stale signature; 400 = malformed JSON or missing signature fields;
  **200-drop** ONLY for a VERIFIED-but-unprocessable event (ignored event
  type, unparseable ``v:broadcast_id``, no matching recipient, replay).
  No non-2xx response carries a body or any PII.
* **Sticky precedence (W6):** the outcome is applied under a rank lattice
  in a single SELECT-FOR-UPDATE read-modify-write transaction, so
  out-of-order / duplicate redelivery is safe.
* **No ``audit_events`` per event, never log the raw recipient email**
  (W10). Breadcrumbs carry ``broadcast_id`` / event / severity only.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.deps import get_session_factory
from app.models.email_broadcast import EmailBroadcastRecipient
from app.rate_limit import limiter
from app.services.mailgun_webhook import (
    DELIVERY_RANK,
    VERIFY_BAD_SIGNATURE,
    VERIFY_KEY_UNSET,
    VERIFY_OK,
    VERIFY_STALE,
    _MAX_BODY_BYTES,
    map_event,
    verify_signature,
)
from app.redis_client import mark_webhook_token_seen


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

# Extra TTL headroom over the signature freshness window for the replay
# token key. The token can legitimately be re-presented only while the
# signature is still fresh, so freshness-window + a small margin bounds it.
_REPLAY_TTL_MARGIN_S = 60


async def _apply_delivery_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broadcast_id: int,
    recipient: str,
    new_status: str,
    event_ts: float,
) -> None:
    """Correlate the event to its recipient row and apply the outcome under
    the sticky precedence rule (W6/W7), in one committed transaction.

    Lookup is ``(broadcast_id, lower(email))`` against the snapshot email
    column (case-insensitive; the snapshot survives ``user_id`` being nulled
    on user-delete, so it is the correct key). No matching row ⇒ breadcrumb
    + drop. The new status is written ONLY when its rank strictly exceeds the
    current row's rank; equal / lower rank is a no-op (idempotent).
    """
    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(EmailBroadcastRecipient)
                    .where(
                        EmailBroadcastRecipient.broadcast_id == broadcast_id,
                        func.lower(EmailBroadcastRecipient.email)
                        == func.lower(recipient),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

        if not rows:
            # Unknown / foreign broadcast_id, or address mismatch. Breadcrumb
            # WITHOUT the raw email (W10), then the router 200-drops.
            await logger.ainfo(
                "webhook.mailgun.no_match",
                broadcast_id=broadcast_id,
                new_status=new_status,
            )
            return

        if len(rows) > 1:
            # Shouldn't happen — user email is unique — but if it does, apply
            # to all and log the anomaly (W7).
            await logger.awarning(
                "webhook.mailgun.multiple_recipient_rows",
                broadcast_id=broadcast_id,
                match_count=len(rows),
            )

        new_rank = DELIVERY_RANK[new_status]
        changed = False
        for row in rows:
            if new_rank > DELIVERY_RANK.get(row.delivery_status, 0):
                row.delivery_status = new_status
                # Event occurrence time (naive UTC, matching the codebase's
                # datetime convention), distinct from the signature timestamp.
                row.delivery_updated_at = datetime.fromtimestamp(
                    event_ts, timezone.utc
                ).replace(tzinfo=None)
                changed = True

        if changed:
            await db.commit()


@router.post("/mailgun", include_in_schema=False)
@limiter.limit("300/minute")
async def mailgun_webhook(request: Request) -> Response:
    """Ingest one signed Mailgun delivery event. Public, no auth.

    Returns 200 on any verified event (whether or not it changed a row),
    401 on a bad/stale signature, 404 when the signing key is unset (fail
    closed), 400 on malformed JSON / missing signature fields, and 413 on
    an oversized body. No non-2xx response carries a body or PII.
    """
    # Own session factory pulled directly (not via Depends) so this stays a
    # zero-dependency public route — nothing here resolves auth (W11).
    session_factory: async_sessionmaker[AsyncSession] = get_session_factory()

    # 1. Cheap content-length precheck (W3): drop an oversized body before
    #    buffering anything. Cannot be authenticated pre-signature, so this
    #    is a non-2xx reject (413), not a silent 2xx-drop.
    declared_len = request.headers.get("content-length")
    if declared_len is not None:
        try:
            if int(declared_len) > _MAX_BODY_BYTES:
                await logger.ainfo(
                    "webhook.mailgun.oversized",
                    content_length=declared_len,
                )
                return Response(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                )
        except ValueError:
            pass

    # 2. Read the body, capped.
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        await logger.ainfo("webhook.mailgun.oversized", byte_len=len(raw))
        return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    # 3. JSON parse → 400 on failure.
    try:
        payload: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # 4. Pull the signature object → 400 if any field missing.
    sig = payload.get("signature")
    if not isinstance(sig, dict):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    ts = sig.get("timestamp")
    token = sig.get("token")
    signature = sig.get("signature")
    if ts is None or token is None or signature is None:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # 5. Verify HMAC + freshness (W1). Everything below is gated on this.
    result = verify_signature(
        ts,
        token,
        signature,
        signing_key=settings.mailgun_webhook_signing_key,
        tolerance_s=settings.mailgun_webhook_timestamp_tolerance_s,
        now_ts=int(time.time()),
    )
    if result == VERIFY_KEY_UNSET:
        # Feature not enabled — fail closed (W2). Never 2xx an unverifiable
        # payload.
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    if result in (VERIFY_BAD_SIGNATURE, VERIFY_STALE):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    # result == VERIFY_OK — proceed.
    assert result == VERIFY_OK

    # 6. Replay dedup (defense-in-depth; fail-OPEN). Already-seen token ⇒
    #    verified but a replay → 200-drop.
    ttl_s = settings.mailgun_webhook_timestamp_tolerance_s + _REPLAY_TTL_MARGIN_S
    first_sight = await mark_webhook_token_seen(str(token), ttl_s)
    if not first_sight:
        await logger.ainfo("webhook.mailgun.replay_dropped")
        return Response(status_code=status.HTTP_200_OK)

    # 7. Pull event-data. From here every non-processable case is a
    #    VERIFIED 200-drop (W2).
    event_data = payload.get("event-data")
    if not isinstance(event_data, dict):
        return Response(status_code=status.HTTP_200_OK)
    event = event_data.get("event")
    severity = event_data.get("severity")
    recipient = event_data.get("recipient")
    user_vars = event_data.get("user-variables")
    broadcast_id_raw = (
        user_vars.get("broadcast_id") if isinstance(user_vars, dict) else None
    )
    event_ts = event_data.get("timestamp")

    # 8. Map event → status. Unknown / ignored event ⇒ 200-drop.
    new_status = map_event(event, severity) if isinstance(event, str) else None
    if new_status is None:
        await logger.ainfo(
            "webhook.mailgun.ignored_event",
            mg_event=event,
            severity=severity,
        )
        return Response(status_code=status.HTTP_200_OK)

    # 9. Parse v:broadcast_id (echoed as a STRING by Mailgun). Unparseable ⇒
    #    200-drop.
    try:
        broadcast_id = int(broadcast_id_raw)
    except (TypeError, ValueError):
        await logger.ainfo(
            "webhook.mailgun.bad_broadcast_id",
            mg_event=event,
        )
        return Response(status_code=status.HTTP_200_OK)

    if not isinstance(recipient, str) or not recipient:
        await logger.ainfo(
            "webhook.mailgun.missing_recipient",
            broadcast_id=broadcast_id,
            mg_event=event,
        )
        return Response(status_code=status.HTTP_200_OK)

    # ``event-data.timestamp`` is a float epoch (event occurrence time).
    try:
        occurred_ts = float(event_ts)
    except (TypeError, ValueError):
        occurred_ts = time.time()

    # Structured breadcrumb — broadcast_id / event / severity only, NEVER the
    # raw recipient email (W10).
    await logger.ainfo(
        "webhook.mailgun.event",
        broadcast_id=broadcast_id,
        mg_event=event,
        severity=severity,
        new_status=new_status,
    )

    # 10. Apply under precedence (W6/W7), DB last.
    await _apply_delivery_status(
        session_factory,
        broadcast_id=broadcast_id,
        recipient=recipient,
        new_status=new_status,
        event_ts=occurred_ts,
    )
    return Response(status_code=status.HTTP_200_OK)
