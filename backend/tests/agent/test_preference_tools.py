"""TBD-787: the notification-preference tools beyond the honesty fence, F-A3
and the revert round trip (test_agent_honesty, test_actions, test_revert)."""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.agent import preference_tools
from app.services.notification_service import _default_preferences
from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    PREFS_ON, _confirm, _invoke, _prefs, _refused, _rows, engine, factory, w,
)

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


async def test_the_first_save_creates_the_row_and_only_the_person_confirms(factory, w):
    a = w["A"]
    out = await _invoke(factory, a["member"], TOOL, {**PREFS_ON, "email_account": False},
                        api_token_id=a["t1"], channel="mcp", scope="agent:auto")
    assert out["requires_confirmation"] is True  # sensitive: an auto token only stages it
    assert {c["before"] for c in out["changes"]} == {None}
    await _confirm(factory, a["member"], out["action_id"])
    assert await _invoke(factory, a["member"], "notification_preferences_get", {}) == {
        **PREFS_ON, "email_account": False}


def test_only_the_router_is_used_from_the_notifications_module():
    """FENCE (F-R5 exception). Wrong implementation: reaching the module's
    ``notification_service`` (mail) through the reviewed import."""
    tree = ast.parse(pathlib.Path(preference_tools.__file__).read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    froms = [(n.module, [a.name for a in n.names]) for n in imports if isinstance(n, ast.ImportFrom)
             and n.module and "notification" in n.module]
    assert froms == [("app.routers.notifications", ["router"])]
    assert not [a.name for n in imports if isinstance(n, ast.Import) for a in n.names if "notification" in a.name]
    # ``router`` is passed on, never reached into
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "router"]
