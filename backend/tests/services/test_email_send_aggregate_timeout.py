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
async def test_send_batch_returns_false_when_the_send_outlasts_the_budget(
    monkeypatch,
) -> None:
    """Same bound on the broadcast path, whose failure contract the drain
    already reads as "revert these rows to failed and let a resume retry"."""
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

    assert result is False
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_send_batch_control_returns_true_inside_the_budget(
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

    assert result is True
    assert len(seen) == 1
