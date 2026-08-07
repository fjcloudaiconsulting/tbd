"""Aggregate verify deadline for the Turnstile siteverify call (TBD-328).

``app.captcha.verify_captcha`` holds a single ``await client.post`` under
``httpx.Timeout(settings.captcha_verify_timeout_s)``. That bound is *per
phase* — connect / write / read / pool each get the full budget, and
``read`` is charged per socket read rather than per response. A provider
dribbling one byte just under the read budget therefore keeps the call
alive indefinitely without ever tripping it, which is what TBD-179 proved
against a real drip-feed server on the Google OAuth exchange and TBD-266
closed on the Mailgun sends.

This site is worse than either: the call is inline on the synchronous,
public, pre-auth ``POST /api/v1/auth/register`` path, so an unbounded
verify parks a worker per registration attempt on an unauthenticated
endpoint. Availability, not latency.

These fences pin the aggregate ceiling that closes the gap, following
TBD-266's shape (``tests/services/test_email_send_aggregate_timeout.py``):
an ``asyncio`` deadline around the awaited call only, with the status
check, the ``.json()`` parse, the logging and ``aclose()`` outside it.

⚠ The handler difference from TBD-266 is the whole point of this ticket.
``email_service`` already had a surrounding ``except Exception`` that
caught the ``TimeoutError`` ``asyncio.timeout`` raises, so TBD-266 needed
no handler change. ``verify_captcha`` has NO such fallback: its clauses
are ``httpx.TimeoutException`` and ``httpx.HTTPError``, and builtin
``TimeoutError`` is an ``OSError`` subclass descending from neither. A
bound added without widening the timeout clause therefore lets a bare
``TimeoutError`` escape ``verify_captcha`` and surface as an unhandled
500 on ``/register`` — reintroducing exactly the failure TBD-179 existed
to fix, on a worse endpoint. ``test_bare_builtin_timeouterror_is_caught``
and ``test_aggregate_timeout_returns_rather_than_raises`` below are the
fences that must go RED against that half-fix; both are timing-free where
it matters, so neither can be satisfied by the bound alone.

What the fake proves and what it does not: the slow transport stands in
for the drip feed because ``httpx``'s per-phase bound cannot fire against
``MockTransport`` either way. So a passing test here does not by itself
say the aggregate bound is what caught the call — removing the
``asyncio.timeout`` block and watching these go red is what says that.

The delay is always ``await asyncio.sleep(...)`` and never
``time.sleep(...)``: the transport runs on the test's own event loop, so
a blocking sleep would wedge the suite. Tests stay fast by monkeypatching
``settings.captcha_verify_total_timeout_s`` down, not by waiting.

Every timeout assertion is paired with a control on the same code path
that answers inside the budget and must return ``ok=True`` — without it
an implementation that simply always failed closed would pass.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from app import captcha
from app.captcha import (
    REASON_OK,
    REASON_NETWORK_ERROR,
    REASON_TIMEOUT,
    CaptchaVerifyResult,
    verify_captcha,
)
from app.config import Settings
from app.config import settings as app_settings


# Captured before any test patches ``__init__`` so repeated installs always
# wrap the real one (same reasoning as test_email_send_aggregate_timeout.py).
_REAL_ASYNC_CLIENT_INIT = httpx.AsyncClient.__init__

# Comfortably longer than any patched budget below, so a slow CI box cannot
# turn "the bound fired" into a coin flip.
_HANG_S = 1.0
# Comfortably longer than an instant transport, for the same reason.
#
# ⚠ MUST NOT equal ``_PER_PHASE_BUDGET_S``. The two settings are separate
# knobs and the log-value fence below asserts one of each; when both held
# 5.0 those two assertions collapsed into the same number and stopped
# discriminating. Measured against the collided fixture: a handler logging
# the per-phase setting under the aggregate key passed 9/9, and so did one
# hardcoding both kwargs as the literal 5.0 — i.e. the fence's own docstring
# claim ("a hardcoded value would drift from the config and redden here")
# was false. ``test_the_two_budgets_are_distinguishable`` pins the gap so a
# future fixture tidy-up cannot silently re-collide them.
_PER_PHASE_BUDGET_S = 5.0
_CONTROL_BUDGET_S = 7.5
_TIMEOUT_BUDGET_S = 0.05


@pytest.fixture(autouse=True)
def configure_captcha(monkeypatch):
    """Enabled, Turnstile-like config so each test overrides only what it
    needs. Mirrors ``tests/test_captcha.py``'s fixture."""
    monkeypatch.setattr(app_settings, "captcha_required", True)
    monkeypatch.setattr(app_settings, "captcha_provider", "turnstile")
    monkeypatch.setattr(app_settings, "captcha_secret", "test-secret")
    monkeypatch.setattr(
        app_settings,
        "captcha_verify_url",
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    )
    monkeypatch.setattr(
        app_settings, "captcha_verify_timeout_s", _PER_PHASE_BUDGET_S
    )
    monkeypatch.setattr(app_settings, "captcha_expected_hostname", "")
    monkeypatch.setattr(app_settings, "captcha_expected_action", "")
    yield


def _install_slow_transport(monkeypatch, delay_s: float) -> list[httpx.Request]:
    """Wire every ``httpx.AsyncClient`` onto a transport that answers a
    Turnstile success body after ``delay_s`` of *awaited* sleep."""
    seen: list[httpx.Request] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await asyncio.sleep(delay_s)
        return httpx.Response(
            200,
            json={
                "success": True,
                "hostname": "app.thebetterdecision.com",
                "action": "register",
            },
        )

    transport = httpx.MockTransport(_handler)

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return seen


def _install_exploding_transport(monkeypatch, exc: BaseException) -> list[httpx.Request]:
    """Wire every ``httpx.AsyncClient`` onto a transport that raises."""
    seen: list[httpx.Request] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise exc

    transport = httpx.MockTransport(_handler)

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return seen


def _record_logs(monkeypatch) -> list[tuple[str, dict]]:
    """Swap ``captcha.logger`` for a recorder returning ``(event, kwargs)``."""
    captured: list[tuple[str, dict]] = []

    class _Recorder:
        async def ainfo(self, event: str, **kw) -> None:
            captured.append((event, kw))

        async def awarning(self, event: str, **kw) -> None:
            captured.append((event, kw))

        async def aerror(self, event: str, **kw) -> None:
            captured.append((event, kw))

    monkeypatch.setattr(captcha, "logger", _Recorder())
    return captured


# ─── the setting ───
#
# There is deliberately NO test here reading
# ``Settings.model_fields[...].default`` to assert aggregate >= per_phase.
# One existed and was deleted in review: once the validator below enforces
# the relationship at construction, a violating DEFAULT makes the
# module-scope ``settings = Settings()`` raise, so this file cannot even be
# collected — the assertion can never be reached in a state where it would
# fail. A fence that cannot fire is decoration that reads as coverage.
#
# The property is still protected twice over: by that collection-time
# explosion, and by the validator fences below, which go red the moment the
# validator is removed. The derivation it used to document (why the floor is
# ONE per-phase read budget rather than a sum, unlike TBD-179's two-call
# exchange) now lives in the comment above the setting in ``config.py``.


def test_the_two_budgets_are_distinguishable() -> None:
    """Guard the fixture invariant the log-value fence depends on.

    ``test_bare_builtin_timeouterror_keeps_the_existing_event`` asserts one
    setting per log key. If the fixture ever gives both settings the same
    number those two assertions stop discriminating and the fence goes
    vacuous — measured, not hypothetical: with both at 5.0 a handler
    logging the per-phase value under the aggregate key passed the whole
    file, and so did one hardcoding both kwargs as literals.

    This is deliberately a separate test rather than an assert inside that
    fence, so the failure names the *cause* (the fixture) instead of
    reddening as a confusing value mismatch in the handler test.
    """
    assert _PER_PHASE_BUDGET_S != _CONTROL_BUDGET_S


# ─── the settings validator ───
#
# ``0 < per_phase <= total``, enforced at construction. Read the LIVE
# values, which is the whole point: the default-reading fence above cannot
# see an env override, and an override is the only way this pair goes wrong
# in production. Constructed with ``_env_file=None`` so a developer's local
# ``.env`` cannot decide whether these pass.


def test_aggregate_below_per_phase_is_refused_at_construction() -> None:
    """The gap the default-reading fence cannot cover.

    ``CAPTCHA_VERIFY_TOTAL_TIMEOUT_S`` below ``CAPTCHA_VERIFY_TIMEOUT_S``
    makes the aggregate the binding constraint while the operator is
    looking at the per-phase knob — the silent, fail-closed split this
    ticket exists to prevent, arriving through env vars instead of through
    a module constant.
    """
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            jwt_secret_key="x" * 64,
            captcha_verify_timeout_s=30.0,
            captcha_verify_total_timeout_s=20.0,
        )


@pytest.mark.parametrize("total", [0.0, -5.0])
def test_non_positive_aggregate_is_refused_at_construction(total: float) -> None:
    """A zero or negative aggregate is 100% signup failure on a clean boot.

    Measured before the validator existed: both values were ACCEPTED, and
    ``asyncio.timeout(0.0)`` / ``asyncio.timeout(-5.0)`` trip immediately —
    so every verify returns ``REASON_TIMEOUT`` and the fail-closed gate
    refuses every registration, with the app healthy and nothing in the
    logs but a captcha timeout an operator reads as a provider outage. A
    templating bug rendering an unset var as "0" is enough to get here.

    ``0.0`` is the important leg: it is what an unset-but-rendered var
    produces, and it is *not* caught by the ``total < per_phase`` rule
    alone at every per-phase value — it needs the positivity check.
    """
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            jwt_secret_key="x" * 64,
            captcha_verify_timeout_s=5.0,
            captcha_verify_total_timeout_s=total,
        )


def test_non_positive_per_phase_is_refused_at_construction() -> None:
    """Same defect on the other knob. Fenced separately because
    ``total >= per_phase`` is satisfied by ``0 <= 0`` and by every negative
    pair that happens to be ordered — an implementation checking only the
    ordering passes those while still shipping a budget httpx fails on."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            jwt_secret_key="x" * 64,
            captcha_verify_timeout_s=0.0,
            captcha_verify_total_timeout_s=20.0,
        )


def test_valid_timeout_pair_is_accepted() -> None:
    """Control. Without it a validator that rejected EVERY pair — including
    the shipped defaults, i.e. a validator that bricks the boot — would pass
    every assertion above.
    """
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        jwt_secret_key="x" * 64,
        captcha_verify_timeout_s=5.0,
        captcha_verify_total_timeout_s=20.0,
    )
    assert s.captcha_verify_timeout_s == 5.0
    assert s.captcha_verify_total_timeout_s == 20.0


def test_equal_bounds_are_accepted() -> None:
    """The boundary is ``<=``, not ``<``. Pinned from the permitted side so
    a tightening to strict inequality — which would reject the perfectly
    reasonable "one phase, one call" configuration — cannot land silently.
    """
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        jwt_secret_key="x" * 64,
        captcha_verify_timeout_s=7.0,
        captcha_verify_total_timeout_s=7.0,
    )
    assert s.captcha_verify_total_timeout_s == 7.0


def test_shipped_defaults_satisfy_the_validator() -> None:
    """The deploy-safety claim, asserted rather than reasoned about.

    This validator is boot-fatal, and the DO PRE_DEPLOY migrate job binds
    no ``CAPTCHA_*`` value (only APP_ENV, DATABASE_URL, JWT_SECRET_KEY,
    API_TOKEN_HMAC_KEY), so it constructs ``Settings`` on these defaults.
    If they ever stop satisfying the rule, the migrate job crashes on
    import and the deploy breaks before a single migration runs — the
    2026-07-21 ``API_TOKEN_HMAC_KEY`` failure mode. Fail here instead.
    """
    s = Settings(_env_file=None, jwt_secret_key="x" * 64)  # type: ignore[call-arg]
    assert 0 < s.captcha_verify_timeout_s <= s.captcha_verify_total_timeout_s


# ─── the bound ───


@pytest.mark.asyncio
async def test_verify_outlasting_the_budget_fails_closed(monkeypatch) -> None:
    """A verify that never finishes must fail closed, not hold the worker."""
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _TIMEOUT_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=_HANG_S)
    captured = _record_logs(monkeypatch)

    result = await verify_captcha("token", "203.0.113.7")

    assert result.ok is False
    assert result.reason == REASON_TIMEOUT
    # The bound has to fire on a request that actually left the process; a
    # short-circuit before httpx would satisfy the lines above for free.
    assert len(seen) == 1
    # The ONE test in this file where a REAL ``asyncio.timeout`` fires, as
    # opposed to an injected ``TimeoutError``. So this is the only place
    # that can say the aggregate arm is reached by the aggregate bound
    # itself rather than merely by an exception type chosen in a test.
    events = [kw for e, kw in captured if e == "captcha.verify.timeout"]
    assert len(events) == 1, captured
    assert events[0]["bound"] == "aggregate"


@pytest.mark.asyncio
async def test_verify_control_returns_ok_inside_the_budget(monkeypatch) -> None:
    """Control for the fence above: same path, same bound, fast provider.

    Without this an implementation that always returned ``ok=False`` would
    pass every timeout assertion in this file.
    """
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _CONTROL_BUDGET_S
    )
    seen = _install_slow_transport(monkeypatch, delay_s=0.0)

    result = await verify_captcha("token", "203.0.113.7")

    assert result.ok is True
    assert result.reason == REASON_OK
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_aggregate_timeout_returns_rather_than_raises(monkeypatch) -> None:
    """The bound must not turn a slow provider into a 500 on ``/register``.

    ``verify_captcha``'s contract (module docstring: "never raises") is what
    the register handler depends on — ``routers/auth.py`` calls it with no
    ``try``, and ``main.py`` registers no generic ``Exception`` handler, so
    anything escaping here is an unhandled 500 on a public, pre-auth,
    rate-limited-to-5/hour endpoint.

    Asserted as an explicit *value* comparison rather than "did not raise",
    so a half-fix that adds the bound without widening the timeout clause
    reddens on the assertion rather than merely erroring somewhere.
    """
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _TIMEOUT_BUDGET_S
    )
    _install_slow_transport(monkeypatch, delay_s=_HANG_S)

    result = await verify_captcha("token", None)

    assert isinstance(result, CaptchaVerifyResult)
    assert result == CaptchaVerifyResult(ok=False, reason=REASON_TIMEOUT)


# ─── the handler ───
#
# Timing-free. ``asyncio.timeout`` raises a bare builtin ``TimeoutError``,
# which is an ``OSError`` subclass and derives from NEITHER
# ``httpx.TimeoutException`` nor ``httpx.HTTPError``. Injecting one
# directly pins the widened clause independently of whether the aggregate
# bound fires at all, so this fence cannot be satisfied by the bound.


@pytest.mark.asyncio
async def test_bare_builtin_timeouterror_is_caught(monkeypatch) -> None:
    """THE fence for TBD-328's trap: a bare ``TimeoutError`` must be mapped
    to ``REASON_TIMEOUT``, not allowed to escape.

    Goes RED against an implementation with the aggregate bound but the
    original ``except httpx.TimeoutException:`` clause.
    """
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _CONTROL_BUDGET_S
    )
    seen = _install_exploding_transport(monkeypatch, TimeoutError())

    result = await verify_captcha("token", None)

    assert result == CaptchaVerifyResult(ok=False, reason=REASON_TIMEOUT)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_bare_builtin_timeouterror_keeps_the_existing_event(
    monkeypatch,
) -> None:
    """Outcome AND operator signal unchanged: same ``captcha.verify.timeout``
    event, not a new one, so existing log consumers keep working.

    ``total_timeout_s`` is additive and required: an aggregate trip logging
    only ``timeout_s`` would assert that the per-phase bound fired, which is
    an active falsehood in the one log line the incident needs.

    ``bound`` carries what the stable event name cannot. Its counterpart
    assertion lives in ``test_per_phase_httpx_timeout_...`` below and
    demands the OTHER value off the same key, so a handler reporting one
    constant for both trips reddens one of the pair whichever constant it
    picks.
    """
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _CONTROL_BUDGET_S
    )
    _install_exploding_transport(monkeypatch, TimeoutError())
    captured = _record_logs(monkeypatch)

    result = await verify_captcha("token", None)

    assert result.reason == REASON_TIMEOUT
    events = [(e, kw) for e, kw in captured if e == "captcha.verify.timeout"]
    assert len(events) == 1, captured
    _event, kw = events[0]
    # Read off the settings object, not literals: a hardcoded value in the
    # handler would drift from the config and redden here.
    #
    # These two assertions only discriminate while the two settings hold
    # DIFFERENT numbers — see ``test_the_two_budgets_are_distinguishable``,
    # which fails loudly if a fixture edit ever re-collides them.
    assert kw["timeout_s"] == app_settings.captcha_verify_timeout_s
    assert kw["total_timeout_s"] == app_settings.captcha_verify_total_timeout_s
    assert kw["bound"] == "aggregate"
    assert [e for e, _kw in captured if e == "captcha.verify.network_error"] == []


@pytest.mark.asyncio
async def test_per_phase_httpx_timeout_still_lands_on_the_same_reason(
    monkeypatch,
) -> None:
    """Control: widening the clause must not change the httpx per-phase
    path. ``httpx.ReadTimeout`` derives from ``httpx.TimeoutException``, not
    from builtin ``TimeoutError``, so it keeps landing in the same clause.

    Also the discriminating half of the ``bound`` pair. Sharing one clause
    keeps ``captcha.verify.timeout`` stable for existing consumers, but it
    also merges two failures that want different Cloudflare remediations: a
    stalled phase and a drip feed. This asserts ``per_phase`` where
    ``test_bare_builtin_timeouterror_keeps_the_existing_event`` asserts
    ``aggregate``, so no single hardcoded value satisfies both.
    """
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _CONTROL_BUDGET_S
    )
    _install_exploding_transport(monkeypatch, httpx.ReadTimeout("per-phase read"))
    captured = _record_logs(monkeypatch)

    result = await verify_captcha("token", None)

    assert result == CaptchaVerifyResult(ok=False, reason=REASON_TIMEOUT)
    events = [kw for e, kw in captured if e == "captcha.verify.timeout"]
    assert len(events) == 1, captured
    assert events[0]["bound"] == "per_phase"


@pytest.mark.asyncio
async def test_non_timeout_transport_error_is_not_renamed_a_timeout(
    monkeypatch,
) -> None:
    """Control for the whole handler block: an implementation that called
    everything a timeout would pass every fence above. A connect failure is
    still ``network_error``.
    """
    monkeypatch.setattr(
        app_settings, "captcha_verify_total_timeout_s", _CONTROL_BUDGET_S
    )
    _install_exploding_transport(monkeypatch, httpx.ConnectError("nope"))
    captured = _record_logs(monkeypatch)

    result = await verify_captcha("token", None)

    assert result == CaptchaVerifyResult(ok=False, reason=REASON_NETWORK_ERROR)
    assert [e for e, _kw in captured if e == "captcha.verify.timeout"] == []
    assert [e for e, _kw in captured if e == "captcha.verify.network_error"] != []
