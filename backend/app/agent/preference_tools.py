"""The caller's own notification preferences (TBD-787): one read, one change.

These are ordinary user settings, not a security surface: the security
category cannot be switched off on either channel (the route refuses
``email_security=false`` and the service forces both security columns on),
so the change refuses a false for either instead of previewing a change the
route would not make. The PUT replaces every toggle, so every one is
required; the preview lists only the ones that change. The change is
``sensitive`` (browser-only confirm, never auto): silencing a person's alerts
always needs the person.

The read is hand-written: the GET creates the row on first use, and a read
tool writes nothing (F-R7). With no row it answers the defaults the route
would create (every toggle on; pinned against the service by
``tests/agent/test_preference_tools.py``).

Only ``router`` is imported from the notifications router (a reviewed F-R5
exception; ``tests/agent/test_preference_tools.py`` pins that nothing else of
that module is used), and the row is read through the table, not the model.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models.base import Base
from app.models.user import Role
from app.routers.notifications import router

PATH = "/api/v1/notifications/preferences"
TABLE = "user_notification_preferences"
FIELDS = (
    "email_security", "email_account", "email_org_admin", "email_org_activity", "email_cc_statement",
    "in_app_security", "in_app_account", "in_app_org_admin", "in_app_org_activity", "in_app_cc_statement",
)
DEFAULTS = dict.fromkeys(FIELDS, True)
SECURITY_ON = {"email_security": True, "in_app_security": True}


def _row(ctx: ToolContext, *, lock: bool = False):
    t = Base.metadata.tables[TABLE]
    q = select(*(t.c[f] for f in FIELDS)).where(t.c.user_id == ctx.user.id)
    return ctx.db.execute(q.with_for_update() if lock else q)


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _read(ctx: ToolContext, _args) -> dict:
    row = (await _row(ctx)).mappings().first()
    # The route forces both security toggles on at read time too.
    return DEFAULTS if row is None else {**{f: row[f] for f in FIELDS}, **SECURITY_ON}


register(ToolSpec(
    name="notification_preferences_get", risk="read", args=_NoArgs, product_area=None,
    min_role=Role.MEMBER, mirrors_route=("GET", PATH), run=_read,
    description="Read this user's notification preferences: one on/off toggle per category and channel.",
))


async def _preview(ctx: ToolContext, args) -> Preview:
    if not (args.email_security and args.in_app_security):
        raise ToolError("security_emails_required", "Security notifications cannot be disabled.")
    row = (await _row(ctx, lock=True)).mappings().first()
    # No row yet: the route inserts one holding exactly these values.
    changes = [
        Change(TABLE, ctx.user.id, f, None if row is None else row[f], getattr(args, f))
        for f in FIELDS if row is None or row[f] != getattr(args, f)
    ]
    if not changes:
        raise ToolError("no_change", "the notification preferences already have these values")
    off = [c.field for c in changes if c.after is False]
    return Preview(
        summary=f"Change {len(changes)} notification preference(s)", changes=changes,
        warnings=[f"Turns off: {', '.join(off)}."] if off else [],
    )


register(route_tool(
    name="notification_preferences_update", risk="sensitive",
    router=router, method="PUT", path=PATH,
    fields=FIELDS, required=FIELDS, product_area=None, min_role=Role.MEMBER,
    description=(
        "Set this user's notification preferences. Send every toggle (read them first with "
        "notification_preferences_get); email_security and in_app_security must stay true, "
        f"because security notifications cannot be turned off. {CONFIRM}"
    ),
    preview=_preview,
))
