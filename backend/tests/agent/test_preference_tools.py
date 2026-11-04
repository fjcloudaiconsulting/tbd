"""TBD-787: the notification-preference tools beyond the honesty fence, F-A3
and the revert round trip (test_agent_honesty, test_actions, test_revert)."""
from __future__ import annotations

import pytest

from app.agent import preference_tools
from app.services.notification_service import _default_preferences
from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    PREFS_ON, _confirm, _invoke, _prefs, _refused, _rows, engine, factory, w,
)
from tests.agent.test_revert import _revert

TOOL = "notification_preferences_update"


@pytest.mark.parametrize("field", ["email_security", "in_app_security"])
async def test_security_notifications_cannot_be_switched_off(factory, w, field):
    """FENCE. Wrong implementations: staging ``in_app_security=false``, which
    the service silently writes back as true (a preview that lies), or any
    path that lets an agent silence security alerts."""
    a = w["A"]
    await _prefs(factory, a)
    err = await _refused(_invoke(factory, a["member"], TOOL, {**PREFS_ON, field: False}))
    assert err.code == "security_emails_required"
    assert await _rows(factory) == []


def test_the_read_defaults_are_the_routes():
    """GUARD. With no row the read answers what the route would create."""
    row = _default_preferences(1)
    assert preference_tools.DEFAULTS == {f: getattr(row, f) for f in preference_tools.FIELDS}


async def test_the_read_answers_the_row_and_unchanged_values_are_no_change(factory, w):
    a = w["A"]
    assert await _invoke(factory, a["member"], "notification_preferences_get", {}) == PREFS_ON
    await _prefs(factory, a)
    err = await _refused(_invoke(factory, a["member"], TOOL, PREFS_ON))
    assert err.code == "no_change"


async def test_the_first_save_creates_the_row_and_is_not_revertible(factory, w):
    a = w["A"]
    out = await _invoke(factory, a["member"], TOOL, {**PREFS_ON, "email_account": False})
    assert {c["before"] for c in out["changes"]} == {None}
    done = await _confirm(factory, a["member"], out["action_id"])
    assert await _invoke(factory, a["member"], "notification_preferences_get", {}) == {
        **PREFS_ON, "email_account": False}
    err = await _refused(_revert(factory, a["member"], done["action_id"]))
    assert err.code == "not_revertible"
