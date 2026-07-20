"""Mailgun delivery-webhook signature verification.

Pure logic only — no I/O. The caller injects ``now_ts`` so freshness
checks are deterministic and unit-testable without patching the clock.

See ``specs/2026-07-20-mailgun-delivery-webhooks-design.md`` Ruling W1
for the exact contract. Key points that are load-bearing:

* The signing key is the domain's **HTTP webhook signing key**
  (``settings.mailgun_webhook_signing_key``), a NEW secret distinct from
  ``mailgun_api_key``. Never log it.
* **Key unset ⇒ fail CLOSED** (``"key_unset"`` → the router maps this to
  404). There is deliberately NO path where an empty key disables
  verification and accepts events — this inverts ``send_email``'s
  empty-key dev no-op, because an inbound security endpoint must fail
  closed.
* The comparison uses ``hmac.compare_digest`` (constant time), never
  ``==`` — a plain equality compare leaks timing information about how
  many leading bytes matched, which a patient attacker can use to forge
  a signature byte-by-byte.
"""

import hashlib
import hmac

# Outcome tokens returned by ``verify_signature``. The router maps these
# to HTTP status (W2): ``key_unset`` → 404, ``bad_signature`` / ``stale``
# → 401, ``ok`` → continue processing.
VERIFY_OK = "ok"
VERIFY_BAD_SIGNATURE = "bad_signature"
VERIFY_STALE = "stale"
VERIFY_KEY_UNSET = "key_unset"


def verify_signature(
    timestamp,
    token,
    signature,
    *,
    signing_key: str,
    tolerance_s: int,
    now_ts: int,
) -> str:
    """Verify a Mailgun webhook signature. Pure — no I/O.

    Inputs come from the JSON body's ``signature`` object
    ``{timestamp, token, signature}`` (NOT headers). ``now_ts`` is the
    current epoch second, injected by the caller so this stays
    deterministic.

    Returns one of:
      * ``"key_unset"`` — ``signing_key`` is empty. FAIL CLOSED: the
        feature is not configured, reject every payload (router → 404).
      * ``"bad_signature"`` — ``timestamp`` is non-int / None, or the
        HMAC does not match (router → 401).
      * ``"stale"`` — ``abs(now_ts - int(timestamp)) > tolerance_s``
        (past OR future beyond the skew window; router → 401).
      * ``"ok"`` — signature valid and fresh; proceed with processing.
    """
    if not signing_key:
        # FAIL CLOSED (W1). Empty key means the webhook feature is not
        # enabled; never accept an unverified event.
        return VERIFY_KEY_UNSET
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return VERIFY_BAD_SIGNATURE
    if abs(now_ts - ts) > tolerance_s:
        # Past OR future beyond the skew window — replay-mitigation
        # freshness gate (W1).
        return VERIFY_STALE
    # A crafted body can make ``signature`` any JSON type; a non-str would
    # raise TypeError in compare_digest below and surface as a 500 on this
    # public endpoint (W2 says unauth input must be 4xx, never 5xx). Reject
    # it as a bad signature here.
    if not isinstance(signature, str):
        return VERIFY_BAD_SIGNATURE
    expected = hmac.new(
        signing_key.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    # Constant-time compare (W1) — MUST NOT use ``==``.
    if hmac.compare_digest(expected, signature):
        return VERIFY_OK
    return VERIFY_BAD_SIGNATURE


# ── Event → delivery_status mapping + sticky precedence (W5/W6) ─────────
#
# Modern Mailgun webhooks POST ONE event per request. The router maps the
# raw ``event`` (+ ``severity`` for ``failed``) to one of the four
# delivery outcomes, or ``None`` to signal "ignore this event" (200-drop).

# Hard cap on the request body this public route will buffer. Mailgun's
# ``event-data`` can carry full message headers, so 16 KiB (the CSP sink's
# cap) would truncate legitimate events; 256 KiB is generous headroom
# while still bounding memory on this unauthenticated route (W3).
_MAX_BODY_BYTES = 256 * 1024  # 256 KiB

# Sticky precedence lattice (W6). Apply a new status ONLY when its rank is
# strictly greater than the current row's rank, so terminal-negative
# outcomes (complained / permanent bounce) survive a late or duplicate
# ``delivered`` redelivery, while a real ``delivered`` still overrides an
# earlier soft (temporary) bounce. NULL (no status yet) ranks lowest.
DELIVERY_RANK: dict[str | None, int] = {
    None: 0,
    "bounced_temporary": 1,
    "delivered": 2,
    "bounced_permanent": 3,
    "complained": 4,
}


def map_event(event: str, severity: str | None) -> str | None:
    """Map a Mailgun ``event`` (+ ``severity``) to a delivery status.

    Returns one of ``delivered`` / ``bounced_permanent`` /
    ``bounced_temporary`` / ``complained``, or ``None`` for any event we
    do not record (opened / clicked / unsubscribed / unknown → the router
    200-drops).

    ``failed`` with a missing or unknown severity is treated as a
    PERMANENT bounce (W5): surface it as actionable rather than silently
    downgrading to a soft bounce. The raw severity is logged by the caller.
    """
    if event == "delivered":
        return "delivered"
    if event == "failed":
        # temporary → soft bounce; permanent / missing / unknown → hard.
        if severity == "temporary":
            return "bounced_temporary"
        return "bounced_permanent"
    if event == "complained":
        return "complained"
    return None
