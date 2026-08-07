"""Aggregate send deadline for the two Mailgun HTTP calls (TBD-266).

``email_service.send_email`` and ``email_service.send_batch`` each hold a
single ``await client.post`` under ``httpx.Timeout(10.0)``. That bound is
*per phase* — connect / write / read / pool each get the full budget, and
``read`` is charged per socket read rather than per response. A server
dribbling one byte just under the read budget therefore keeps the call
alive indefinitely without ever tripping it, which is what TBD-179 proved
against a real drip-feed server on the Google OAuth exchange.

These fences pin the aggregate ceiling that closes that gap, following
TBD-179's shape (``tests/routers/test_auth_google_callback_errors.py``):
an ``asyncio`` deadline around the awaited call only, with the existing
``except Exception`` catching the resulting ``TimeoutError``. On 3.11+
``asyncio.TimeoutError`` *is* the builtin ``TimeoutError``, an ``OSError``
subclass, so that handler already catches it — no new ``except`` clause.

What the fake proves and what it does not: the slow transport stands in
for the drip feed because ``httpx``'s per-phase bound cannot fire against
``MockTransport`` either way. So a passing test here does not by itself
say the aggregate bound is what caught the send — removing the
``asyncio.timeout`` block and watching these go red is what says that.

The delay is always ``await asyncio.sleep(...)`` and never
``time.sleep(...)``: the transport runs on the test's own event loop, so
a blocking sleep would wedge the suite. Tests stay fast by monkeypatching
``email_service.MAILGUN_SEND_TOTAL_TIMEOUT_S`` down, not by waiting.

Every timeout assertion is paired with a control on the same code path
that answers inside the budget and must return the *success* value —
without it an implementation that simply always failed would pass.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import email_service
from app.services.email_service import SendDisposition

# Captured before any test patches ``__init__`` so repeated installs always
# wrap the real one (same reasoning as test_broadcast_batch_primitives.py).
_REAL_ASYNC_CLIENT_INIT = httpx.AsyncClient.__init__

# Comfortably longer than any patched budget below, so a slow CI box cannot
# turn "the bound fired" into a coin flip.
_HANG_S = 1.0
# Comfortably shorter than the control budget, for the same reason.
_CONTROL_BUDGET_S = 5.0
_TIMEOUT_BUDGET_S = 0.05


def _install_slow_transport(monkeypatch, delay_s: float) -> list[httpx.Request]:
    """Wire every ``httpx.AsyncClient`` onto a transport that answers 200
    after ``delay_s`` seconds of *awaited* sleep. Returns the request log."""
    seen: list[httpx.Request] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await asyncio.sleep(delay_s)
        return httpx.Response(200, json={"id": "<queued>", "message": "Queued"})

    transport = httpx.MockTransport(_handler)

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return seen


def _record_logs(monkeypatch) -> list[tuple[str, dict]]:
    """Swap ``email_service.logger`` for a recorder. Returns the event log
    as ``(event_name, kwargs)`` pairs, same shape test_email_templates uses."""
    captured: list[tuple[str, dict]] = []

    class _Recorder:
        async def ainfo(self, event: str, **kw) -> None:
            captured.append((event, kw))

        async def aerror(self, event: str, **kw) -> None:
            captured.append((event, kw))

    monkeypatch.setattr(email_service, "logger", _Recorder())
    return captured


def _install_exploding_transport(monkeypatch, exc: BaseException) -> None:
    """Wire every ``httpx.AsyncClient`` onto a transport that raises."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    transport = httpx.MockTransport(_handler)

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)


def _install_status_transport(monkeypatch, status: int) -> list[httpx.Request]:
    """Wire every ``httpx.AsyncClient`` onto a transport answering ``status``
    immediately. Returns the request log, so a fence can prove the call left
    the process rather than being short-circuited before httpx."""
    seen: list[httpx.Request] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"message": "canned"})

    transport = httpx.MockTransport(_handler)

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return seen


def _prod_mailgun(monkeypatch) -> None:
    """Take both send paths out of dev mode so they reach httpx at all."""
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "key-123")
    monkeypatch.setattr(email_service.settings, "mailgun_domain", "mg.example.com")
    monkeypatch.setattr(email_service.settings, "mailgun_region", "eu")
    monkeypatch.setattr(
        email_service.settings, "email_from", "TBD <no-reply@mg.example.com>"
    )


# ─── the constant ───


def test_aggregate_budget_is_not_narrower_than_one_per_phase_read() -> None:
    """Pin the derivation of ``MAILGUN_SEND_TOTAL_TIMEOUT_S``.

    Each send is ONE call, so the floor is one per-phase read budget, not
    two as in TBD-179's two-call exchange. Tighten the aggregate below
    that (or raise the per-phase value without raising the aggregate) and
    healthy sends start failing closed — password resets and verification
    mails silently returning ``False`` — with nothing else in the suite
    noticing, because every other send test answers instantly.
    """
    assert (
        email_service.MAILGUN_SEND_TOTAL_TIMEOUT_S
        >= email_service.MAILGUN_TIMEOUT.read
    )


# ─── send_email ───


@pytest.mark.asyncio
async def test_send_email_returns_false_when_the_send_outlasts_the_budget(
    monkeypatch,
) -> None:
    """A send that never finishes must fail closed, not hang the caller."""
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _TIMEOUT_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=_HANG_S)

    result = await email_service.send_email(
        "alice@acme.io", "Reset your password", "<p>hi</p>"
    )

    assert result is False
    # The bound has to fire on a send that actually left the process; a
    # short-circuit before httpx would satisfy the line above for free.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_send_email_control_returns_true_inside_the_budget(
    monkeypatch,
) -> None:
    """Control for the fence above: same path, same bound, fast server."""
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _CONTROL_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=0.0)

    result = await email_service.send_email(
        "alice@acme.io", "Reset your password", "<p>hi</p>"
    )

    assert result is True
    assert len(seen) == 1


# ─── send_batch ───


@pytest.mark.asyncio
async def test_send_batch_is_indeterminate_when_the_send_outlasts_the_budget(
    monkeypatch,
) -> None:
    """Same bound on the broadcast path.

    This docstring used to read "whose failure contract the drain already
    reads as *revert these rows to failed and let a resume retry*". That
    sentence WAS the TBD-330 bug, recorded as intended behaviour and green
    in CI: the aggregate bound fires with the request already on the wire,
    so "let a resume retry" is an instruction to duplicate real customer
    email. The bound still has to fire — that part was always right — but
    what it produces is an UNKNOWN, never a failure.
    """
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _TIMEOUT_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=_HANG_S)

    result = await email_service.send_batch(
        ["alice@acme.io"],
        "An update",
        "<p>hi</p>",
        "hi",
        {},
        broadcast_id=7,
    )

    assert result.disposition is SendDisposition.INDETERMINATE
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_send_batch_control_is_accepted_inside_the_budget(
    monkeypatch,
) -> None:
    """Control for the fence above."""
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _CONTROL_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=0.0)

    result = await email_service.send_batch(
        ["alice@acme.io"],
        "An update",
        "<p>hi</p>",
        "hi",
        {},
        broadcast_id=7,
    )

    assert result.disposition is SendDisposition.ACCEPTED
    assert len(seen) == 1


# ─── what the timeout actually SAYS ───
#
# ``asyncio.timeout`` raises a BARE ``TimeoutError()`` and
# ``str(TimeoutError())`` is ``""``. Routed through the shared
# ``except Exception`` handler the incident this whole bound exists to
# survive emits ``error=""`` — a blank reason, indistinguishable from any
# other zero-message exception and impossible to alert on. That matters
# more than a normal log nit here because ``routers/auth.py`` dispatches
# the password-reset send through ``BackgroundTasks``, which discards the
# ``False`` return: the structured log is the ONLY operator signal.
#
# Each fence below is paired with a control that drives a NON-timeout
# failure down the same path. Without it a fence would be satisfied by an
# implementation that named everything a timeout, and it also pins that
# the dedicated clause cannot steal httpx's own per-phase timeouts (none
# of httpx's timeout classes derive from builtin ``TimeoutError``).


@pytest.mark.asyncio
async def test_send_email_timeout_names_the_event_and_reports_the_budget(
    monkeypatch,
) -> None:
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _TIMEOUT_BUDGET_S
    )
    _install_slow_transport(monkeypatch, delay_s=_HANG_S)
    captured = _record_logs(monkeypatch)

    result = await email_service.send_email(
        "alice@acme.io",
        "Reset your password",
        '<a href="http://x/reset-password?token=SECRET_RESET">x</a>',
        "http://x/reset-password?token=SECRET_RESET",
    )

    assert result is False
    events = [(e, kw) for e, kw in captured if e == "email_send_timeout"]
    assert len(events) == 1, captured
    _event, kw = events[0]
    # A machine-readable, NON-EMPTY reason. ``str(TimeoutError())`` is "",
    # so this is exactly the assertion the shared handler cannot satisfy.
    assert kw["error"] == "timeout"
    # Read off the module attribute, not a literal: a hardcoded 20.0 in the
    # handler would drift from the constant and redden here.
    assert kw["timeout_s"] == email_service.MAILGUN_SEND_TOTAL_TIMEOUT_S
    # Same PII discipline as ``email_send_failed``: to + subject, never the
    # body, which carries the raw reset token.
    assert set(kw) == {"to", "subject", "error", "timeout_s"}
    assert "SECRET_RESET" not in repr(kw)


@pytest.mark.asyncio
async def test_send_email_non_timeout_failure_still_lands_on_the_failed_event(
    monkeypatch,
) -> None:
    """Control: a transport error is NOT a timeout and must not be named one."""
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _CONTROL_BUDGET_S
    )
    _install_exploding_transport(monkeypatch, httpx.ReadTimeout("per-phase read"))
    captured = _record_logs(monkeypatch)

    result = await email_service.send_email(
        "alice@acme.io", "Reset your password", "<p>hi</p>"
    )

    assert result is False
    assert [e for e, _kw in captured if e == "email_send_timeout"] == []
    failed = [(e, kw) for e, kw in captured if e == "email_send_failed"]
    assert len(failed) == 1, captured
    _event, kw = failed[0]
    assert kw["error_type"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_send_batch_timeout_names_the_event_and_reports_the_budget(
    monkeypatch,
) -> None:
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _TIMEOUT_BUDGET_S
    )
    _install_slow_transport(monkeypatch, delay_s=_HANG_S)
    captured = _record_logs(monkeypatch)

    result = await email_service.send_batch(
        ["alice@acme.io"],
        "An update",
        "<p>hi</p>",
        "hi",
        {},
        broadcast_id=7,
    )

    assert result.disposition is SendDisposition.INDETERMINATE
    events = [(e, kw) for e, kw in captured if e == "broadcast_batch_timeout"]
    assert len(events) == 1, captured
    _event, kw = events[0]
    assert kw["error"] == "timeout"
    assert kw["timeout_s"] == email_service.MAILGUN_SEND_TOTAL_TIMEOUT_S
    # PII bound MA5: counts and a reason, never an address.
    assert set(kw) == {"count", "subject", "error", "timeout_s"}
    assert "alice@acme.io" not in repr(kw)


@pytest.mark.asyncio
async def test_send_batch_non_timeout_failure_still_lands_on_the_failed_event(
    monkeypatch,
) -> None:
    """Control for the fence above."""
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _CONTROL_BUDGET_S
    )
    _install_exploding_transport(monkeypatch, httpx.ReadTimeout("per-phase read"))
    captured = _record_logs(monkeypatch)

    result = await email_service.send_batch(
        ["alice@acme.io"],
        "An update",
        "<p>hi</p>",
        "hi",
        {},
        broadcast_id=7,
    )

    # A per-phase ReadTimeout is ALSO indeterminate — same epistemic state
    # as the aggregate bound, reached through a different clause.
    assert result.disposition is SendDisposition.INDETERMINATE
    assert [e for e, _kw in captured if e == "broadcast_batch_timeout"] == []
    failed = [(e, kw) for e, kw in captured if e == "broadcast_batch_failed"]
    assert len(failed) == 1, captured
    _event, kw = failed[0]
    assert kw["error_type"] == "ReadTimeout"


# ─── F1 (TBD-330): send_batch CLASSIFIES the outcome ───
#
# A bare boolean cannot say the one thing the drain has to know. ``False``
# was returned both for "Mailgun parsed this and refused it" (nothing was
# queued, re-sending is correct) and for "the request was written and the
# answer never came" (Mailgun may hold a copy, re-sending duplicates it).
# The drain read every falsy return as the first, so a 20s aggregate
# timeout on a 1000-address batch reverted 1000 rows to ``failed`` and
# invited an operator to Resume them into duplicates (TBD-330).
#
# ``send_batch`` therefore returns a typed result carrying a tri-state
# ``disposition``. The classification boundary is pinned from BOTH sides
# below: a conclusive refusal must NOT be indeterminate, and an unanswered
# send must NOT be a rejection. Pinning only one side leaves an
# implementation that calls everything indeterminate (never retries a real
# 4xx) or everything rejected (the original bug) passing.
#
# The trap: httpx's own per-phase timeouts do NOT derive from builtin
# ``TimeoutError`` (see the module note above), so ``except TimeoutError``
# catches ONLY the aggregate bound. ``ReadTimeout`` — the single most
# ambiguous outcome there is, the request written and the answer never
# read — lands in the generic handler. Anything unrecognised must default
# to INDETERMINATE; that is the fail-safe direction under the
# never-double-send invariant.


def _batch(**kw):
    """One ``send_batch`` call with a valid single-address vars map."""
    return email_service.send_batch(
        kw.pop("to_list", ["alice@acme.io"]),
        "An update",
        "<p>hi</p>",
        "hi",
        kw.pop("recipient_variables", {}),
        broadcast_id=7,
    )


@pytest.mark.asyncio
async def test_send_batch_dev_mode_is_accepted(monkeypatch) -> None:
    """The dev-mode no-op is an ACCEPTED send: nothing to retry, and the
    drain must leave the rows ``sent`` exactly as it does in prod."""
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "")

    result = await _batch()

    assert result.disposition is SendDisposition.ACCEPTED


@pytest.mark.asyncio
async def test_send_batch_2xx_is_accepted(monkeypatch) -> None:
    _prod_mailgun(monkeypatch)
    seen = _install_status_transport(monkeypatch, 200)

    result = await _batch()

    assert result.disposition is SendDisposition.ACCEPTED
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_send_batch_vars_mismatch_is_rejected(monkeypatch) -> None:
    """MA2's pre-check refuses to issue any HTTP request at all, so nothing
    can possibly have reached Mailgun. Conclusive, hence REJECTED."""
    _prod_mailgun(monkeypatch)
    seen = _install_status_transport(monkeypatch, 200)

    result = await _batch(
        to_list=["alice@acme.io", "bob@acme.io"],
        recipient_variables={"alice@acme.io": {"first_name_html": "A"}},
    )

    assert result.disposition is SendDisposition.REJECTED
    # Conclusive precisely BECAUSE no request left the process.
    assert seen == []
    assert result.reason


@pytest.mark.asyncio
async def test_send_batch_4xx_is_rejected(monkeypatch) -> None:
    """Mailgun parsed the batch and refused it. Nothing was queued, so a
    resume SHOULD retry it — this is the side of the boundary that a
    default-everything-to-indeterminate implementation would break."""
    _prod_mailgun(monkeypatch)
    seen = _install_status_transport(monkeypatch, 400)

    result = await _batch()

    assert result.disposition is SendDisposition.REJECTED
    assert len(seen) == 1
    # ⚠ NOT ``assert result.reason``. Truthiness pins nothing: TBD-330's
    # spec ordered the generic "send_batch returned a falsy result" string
    # DELETED, and reinstating it as the reason for every outcome passes a
    # truthiness check. The drain writes this string verbatim into
    # ``email_broadcast_recipients.error``, where it is the only thing an
    # operator reads, so its CONTENT is the deliverable.
    assert "send_batch returned a falsy result" not in result.reason
    # A rejection must say the batch was not taken. This is the half of the
    # boundary that licenses a resume to re-send.
    assert "did not accept" in result.reason
    assert "nothing was queued" in result.reason


@pytest.mark.asyncio
async def test_send_batch_connect_error_is_rejected(monkeypatch) -> None:
    """The connection was never established, so the request was never
    written. Conclusive in the same way a 4xx is."""
    _prod_mailgun(monkeypatch)
    _install_exploding_transport(monkeypatch, httpx.ConnectError("refused"))

    result = await _batch()

    assert result.disposition is SendDisposition.REJECTED


def test_httpx_connect_timeout_is_not_a_connect_error_subclass() -> None:
    """Pin the taxonomy claim ``_classify_send_exception``'s comment makes.

    The comment there asserts "``ConnectTimeout`` is NOT a subclass of
    ``ConnectError``; both have to be named". That was untested — and a
    comment asserting a subtlety with nothing holding it is exactly how the
    next edit "simplifies" the tuple down to ``ConnectError`` and silently
    reclassifies two exception types.
    """
    assert not issubclass(httpx.ConnectTimeout, httpx.ConnectError)
    assert not issubclass(httpx.PoolTimeout, httpx.ConnectError)
    # And neither derives from the builtin ``TimeoutError``, so neither is
    # caught by the dedicated aggregate-deadline clause either — they can
    # ONLY be classified by the generic handler's isinstance tuple.
    assert not issubclass(httpx.ConnectTimeout, TimeoutError)
    assert not issubclass(httpx.PoolTimeout, TimeoutError)


@pytest.mark.asyncio
async def test_send_batch_connect_timeout_is_rejected(monkeypatch) -> None:
    """``httpx.ConnectTimeout``: the connection was never established, so
    no bytes were ever written and Mailgun cannot be holding a copy.

    REJECTED is the correct — and the RETRYABLE — answer. Dropping this
    class from the tuple makes it INDETERMINATE, which is the fail-safe
    direction and therefore invisible in production: nothing breaks, real
    batches simply stop being retried after a connect timeout and sit
    ``sent`` forever with nobody re-sending them.
    """
    _prod_mailgun(monkeypatch)
    _install_exploding_transport(monkeypatch, httpx.ConnectTimeout("no route"))

    result = await _batch()

    assert result.disposition is SendDisposition.REJECTED
    assert "nothing was queued" in result.reason


@pytest.mark.asyncio
async def test_send_batch_pool_timeout_is_rejected(monkeypatch) -> None:
    """``httpx.PoolTimeout``: we never got a connection out of the pool, so
    the request was never issued. Conclusive, exactly like ``ConnectTimeout``.
    """
    _prod_mailgun(monkeypatch)
    _install_exploding_transport(monkeypatch, httpx.PoolTimeout("pool full"))

    result = await _batch()

    assert result.disposition is SendDisposition.REJECTED
    assert "nothing was queued" in result.reason


@pytest.mark.asyncio
async def test_send_batch_aggregate_timeout_is_indeterminate(monkeypatch) -> None:
    """The headline TBD-330 case: the aggregate bound fires, the request is
    on the wire, the answer never arrives. Informationally identical to a
    crash mid-call, which R2 already rules is NOT retried."""
    _prod_mailgun(monkeypatch)
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _TIMEOUT_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=_HANG_S)

    result = await _batch()

    assert result.disposition is SendDisposition.INDETERMINATE
    assert len(seen) == 1
    # See the note in ``test_send_batch_4xx_is_rejected``: truthiness pins
    # nothing. The reason must NAME THE DEADLINE that fired, because that
    # is the one fact an operator needs to tell "Mailgun refused this" from
    # "we stopped waiting".
    #
    # ⚠ Asserted against the PATCHED budget, not the literal "20.0s" of the
    # module default. That is deliberate and strictly stronger: it proves
    # the reason interpolates the live
    # ``MAILGUN_SEND_TOTAL_TIMEOUT_S`` rather than carrying a hardcoded
    # number that would silently go stale the day the default changes.
    assert f"{_TIMEOUT_BUDGET_S}s" in result.reason
    assert "send_batch returned a falsy result" not in result.reason
    # And it must say the rows are NOT re-sent — the whole point of the
    # disposition.
    assert "NOT re-sent" in result.reason


@pytest.mark.asyncio
async def test_rejected_and_indeterminate_reasons_are_different_strings(
    monkeypatch,
) -> None:
    """The collapse mutant's executioner.

    F2/F3 in ``test_broadcast_drain.py`` compare the row's ``error``
    against constants DEFINED IN THAT TEST FILE and handed to the mock, so
    they fence pass-through and nothing whatsoever about content. Every
    per-disposition test above reads only its own reason. So an
    implementation that hands the SAME string to every non-ACCEPTED
    outcome — including the exact legacy string TBD-330 ordered deleted —
    satisfies all of them.

    Nothing else makes the two real strings meet. This does: it drives both
    code paths in one test and asserts they do not collapse. ``error`` is
    the only channel through which an operator distinguishes "Mailgun
    refused it, resume will retry" from "we never got an answer, these
    rows will NOT be retried"; those are opposite instructions, and one
    string cannot carry both.
    """
    _prod_mailgun(monkeypatch)

    # Leg 1: a conclusive 4xx rejection.
    _install_status_transport(monkeypatch, 400)
    rejected = await _batch()

    # Leg 2: the aggregate deadline firing, same process, same helper.
    monkeypatch.setattr(
        email_service, "MAILGUN_SEND_TOTAL_TIMEOUT_S", _TIMEOUT_BUDGET_S
    )
    _install_slow_transport(monkeypatch, delay_s=_HANG_S)
    indeterminate = await _batch()

    assert rejected.disposition is SendDisposition.REJECTED
    assert indeterminate.disposition is SendDisposition.INDETERMINATE
    assert rejected.reason != indeterminate.reason, (
        "both dispositions produced the same reason string; the error "
        "column can no longer tell a rejection from an unanswered send"
    )
    # Neither may be the string the spec ordered deleted.
    for reason in (rejected.reason, indeterminate.reason):
        assert "send_batch returned a falsy result" not in reason


@pytest.mark.asyncio
async def test_send_batch_read_timeout_is_indeterminate(monkeypatch) -> None:
    """``httpx.ReadTimeout`` does NOT derive from builtin ``TimeoutError``,
    so it never reaches the dedicated aggregate-timeout clause. An
    implementation that classifies by ``except TimeoutError`` alone
    mis-buckets the most ambiguous outcome in the whole table."""
    _prod_mailgun(monkeypatch)
    _install_exploding_transport(monkeypatch, httpx.ReadTimeout("per-phase read"))

    result = await _batch()

    assert result.disposition is SendDisposition.INDETERMINATE


@pytest.mark.asyncio
async def test_send_batch_5xx_is_indeterminate(monkeypatch) -> None:
    """A 5xx can arrive from a proxy after Mailgun already enqueued the
    message. Not conclusive, so not a rejection."""
    _prod_mailgun(monkeypatch)
    _install_status_transport(monkeypatch, 502)

    result = await _batch()

    assert result.disposition is SendDisposition.INDETERMINATE


@pytest.mark.asyncio
async def test_send_batch_unrecognised_exception_defaults_to_indeterminate(
    monkeypatch,
) -> None:
    """The fail-safe direction. An exception nobody enumerated must never be
    assumed to mean 'Mailgun definitely did not get it'."""
    _prod_mailgun(monkeypatch)
    _install_exploding_transport(monkeypatch, RuntimeError("something new"))

    result = await _batch()

    assert result.disposition is SendDisposition.INDETERMINATE
