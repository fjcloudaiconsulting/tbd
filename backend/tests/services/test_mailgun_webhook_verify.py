"""Unit tests for Mailgun webhook signature verification (Ruling W1) and
the fail-OPEN replay-token helper.

``verify_signature`` is pure: ``now_ts`` is injected so freshness checks
are deterministic without patching the clock. We compute the expected
HMAC in-test with the SAME formula the implementation uses, so a mutation
to the formula (key/msg order, digest) is caught here.
"""

import hashlib
import hmac
import inspect

import pytest

from app import redis_client
from app.services import mailgun_webhook
from app.services.mailgun_webhook import (
    VERIFY_BAD_SIGNATURE,
    VERIFY_KEY_UNSET,
    VERIFY_OK,
    VERIFY_STALE,
    verify_signature,
)

_KEY = "test-webhook-signing-key-0123456789abcdef"
_TOLERANCE = 900
_NOW = 1_700_000_000


def _sign(timestamp: str, token: str, key: str = _KEY) -> str:
    """Compute the Mailgun webhook HMAC exactly as the spec/impl define it:
    ``hmac(key, timestamp+token, sha256).hexdigest()``."""
    return hmac.new(
        key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()


def test_correct_signature_ok():
    ts = str(_NOW)
    token = "a" * 50
    sig = _sign(ts, token)
    assert (
        verify_signature(
            ts,
            token,
            sig,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_OK
    )


def test_wrong_signature_bad():
    ts = str(_NOW)
    token = "b" * 50
    # A valid-length hex string that is not the correct HMAC.
    wrong = "0" * 64
    assert (
        verify_signature(
            ts,
            token,
            wrong,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_BAD_SIGNATURE
    )


def test_signature_from_different_key_bad():
    ts = str(_NOW)
    token = "c" * 50
    # Correctly-formed HMAC, but signed with a DIFFERENT key.
    sig = _sign(ts, token, key="some-other-key-9876543210zyxwvut")
    assert (
        verify_signature(
            ts,
            token,
            sig,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_BAD_SIGNATURE
    )


def test_stale_past_timestamp():
    old_ts = _NOW - (_TOLERANCE + 1)
    token = "d" * 50
    sig = _sign(str(old_ts), token)
    assert (
        verify_signature(
            str(old_ts),
            token,
            sig,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_STALE
    )


def test_stale_future_timestamp():
    future_ts = _NOW + (_TOLERANCE + 1)
    token = "e" * 50
    sig = _sign(str(future_ts), token)
    assert (
        verify_signature(
            str(future_ts),
            token,
            sig,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_STALE
    )


def test_within_tolerance_boundary_ok():
    # Exactly at the tolerance edge is NOT stale (abs diff == tolerance).
    edge_ts = _NOW - _TOLERANCE
    token = "f" * 50
    sig = _sign(str(edge_ts), token)
    assert (
        verify_signature(
            str(edge_ts),
            token,
            sig,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_OK
    )


def test_empty_signing_key_fails_closed():
    ts = str(_NOW)
    token = "g" * 50
    # Even a byte-perfect signature must be rejected when the key is unset:
    # empty key ⇒ key_unset, ALWAYS (fail closed, W1).
    sig = _sign(ts, token, key="")
    assert (
        verify_signature(
            ts,
            token,
            sig,
            signing_key="",
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_KEY_UNSET
    )


def test_non_int_timestamp_bad():
    token = "h" * 50
    assert (
        verify_signature(
            "not-a-number",
            token,
            "whatever",
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_BAD_SIGNATURE
    )


def test_none_timestamp_bad():
    token = "i" * 50
    assert (
        verify_signature(
            None,
            token,
            "whatever",
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_BAD_SIGNATURE
    )


def test_none_signature_bad_not_crash():
    ts = str(_NOW)
    token = "j" * 50
    # A signed, fresh request whose ``signature`` field is None must be a
    # clean bad_signature, not a TypeError.
    assert (
        verify_signature(
            ts,
            token,
            None,
            signing_key=_KEY,
            tolerance_s=_TOLERANCE,
            now_ts=_NOW,
        )
        == VERIFY_BAD_SIGNATURE
    )


def test_uses_compare_digest_not_equality():
    """Guard the constant-time-compare requirement (W1) by inspecting the
    source: it MUST call ``hmac.compare_digest`` and MUST NOT compare the
    expected/provided signatures with ``==``."""
    src = inspect.getsource(mailgun_webhook.verify_signature)
    assert "hmac.compare_digest(" in src
    assert "== signature" not in src
    assert "signature ==" not in src


# ── Replay-token helper: FAIL-OPEN on Redis error ───────────────────────


class _RaisingRedis:
    """Fake whose ``set`` raises a RedisError, to exercise fail-open."""

    async def set(self, *args, **kwargs):
        from redis.exceptions import RedisError

        raise RedisError("boom")


@pytest.mark.asyncio
async def test_mark_webhook_token_seen_fail_open_on_redis_error(monkeypatch):
    monkeypatch.setattr(redis_client, "get_client", lambda: _RaisingRedis())
    # Must NOT propagate; must treat as first-sight (True) so a Redis blip
    # never rejects a legitimately-signed event.
    result = await redis_client.mark_webhook_token_seen("tok-abc", ttl_s=1200)
    assert result is True


@pytest.mark.asyncio
async def test_mark_webhook_token_seen_no_redis_first_sight(monkeypatch):
    # Dev / no Redis configured → dedup disabled → first sight.
    monkeypatch.setattr(redis_client, "get_client", lambda: None)
    assert await redis_client.mark_webhook_token_seen("tok-x", ttl_s=1200) is True


@pytest.mark.asyncio
async def test_mark_webhook_token_seen_first_then_replay(monkeypatch):
    # With the autouse fake Redis, first sight is True, replay is False.
    first = await redis_client.mark_webhook_token_seen("tok-dup", ttl_s=1200)
    second = await redis_client.mark_webhook_token_seen("tok-dup", ttl_s=1200)
    assert first is True
    assert second is False


def test_non_string_signature_is_bad_signature_not_error():
    """A crafted body can set ``signature`` to a non-string (int/list/dict).
    That must return bad_signature, never raise (which would 500 the public
    endpoint — W2 forbids a 5xx on unauth input). Regression for the review
    finding."""
    ts = str(int(_NOW))
    for bad in (12345, ["x"], {"x": 1}, True):
        assert (
            mailgun_webhook.verify_signature(
                ts,
                "tok",
                bad,
                signing_key="k",
                tolerance_s=900,
                now_ts=int(_NOW),
            )
            == VERIFY_BAD_SIGNATURE
        )


# ─── F6 (TBD-330): the delivery_status value set is CLOSED ──────────────


def test_map_event_range_is_exactly_the_ranked_delivery_statuses():
    """``map_event`` is the SOLE producer of ``delivery_status``, and its
    range must stay exactly the ranked, non-NULL set.

    ⚠ Read this before widening ``map_event``. TBD-330 made
    ``broadcast_service._run_drain_loop``'s resume predicate turn on
    ``delivery_status IS NULL`` — in the eligibility SELECT *and* in the
    claim UPDATE — on the strength of one property: EVERY non-NULL value
    means "Mailgun has already reported on this address", so the
    "safe to re-send" set is empty and non-nullness is the whole
    discriminator.

    A new mapping is therefore not a local change. If some future event
    ever maps to a value that does NOT imply "already reported on", the
    resume predicate silently starts suppressing rows it should re-send,
    and nothing else in the suite notices. Widen this assertion only after
    re-reading that predicate.

    (An ``== exact set`` assertion is a known hazard in this repo —
    ``reference_reports_v3_networth_source.md``. It is deliberate here: the
    point is precisely to force a re-read, not to be easy to update.)
    """
    events = [
        ("delivered", None),
        ("failed", "temporary"),
        ("failed", "permanent"),
        ("failed", None),
        ("failed", "who-knows"),
        ("complained", None),
        # Everything Mailgun can POST that we deliberately 200-drop.
        ("accepted", None),
        ("opened", None),
        ("clicked", None),
        ("unsubscribed", None),
        ("rejected", None),
        ("stored", None),
        ("some-future-event", None),
    ]
    produced = {mailgun_webhook.map_event(event, sev) for event, sev in events}

    # ⚠ Pinned against a LITERAL frozen set, not against ``DELIVERY_RANK``.
    #
    # This assertion used to read
    # ``produced - {None} == set(DELIVERY_RANK) - {None}`` — a MUTUAL-CLOSURE
    # check between two structures that live in the same module. It reddens
    # if you widen ``map_event`` alone, but the natural way anyone actually
    # widens this is to touch BOTH (the precedence lattice needs the new
    # rank, or ``_apply_delivery_status`` raises a KeyError on it). Widen
    # both and the two sides grow together, the equality still holds, and
    # the fence that exists to force a re-read of the resume predicate
    # passes silently. Measured: adding a fifth value to both passed 16/16.
    #
    # A literal has no such degree of freedom. Changing this set is an
    # explicit, reviewable edit — which is exactly the "force a re-read"
    # property the docstring above promises.
    assert produced - {None} == frozenset(
        {
            "delivered",
            "bounced_permanent",
            "bounced_temporary",
            "complained",
        }
    )
    # And the rank table must still cover exactly that range plus NULL —
    # a value ``map_event`` can emit but ``DELIVERY_RANK`` does not rank
    # would KeyError in ``_apply_delivery_status``'s precedence compare.
    assert set(mailgun_webhook.DELIVERY_RANK) == produced | {None}
    # ``None`` must remain reachable: it is the 200-drop signal, and the
    # NULL that makes a row re-sendable.
    assert None in produced
