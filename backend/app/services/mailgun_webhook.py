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
    expected = hmac.new(
        signing_key.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    # Constant-time compare (W1) — MUST NOT use ``==``.
    if hmac.compare_digest(expected, signature or ""):
        return VERIFY_OK
    return VERIFY_BAD_SIGNATURE
