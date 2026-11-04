"""The caller's own notification preferences (TBD-787): one read, one write.

These are ordinary user settings, not a security surface: the security
category cannot be switched off on either channel (the route refuses
``email_security=false`` and the service forces both security columns on),
so the write refuses a false for either instead of previewing a change the
route would not make. The PUT replaces every toggle, so every one is
required; the preview lists only the ones that change.

The read is hand-written: the GET creates the row on first use, and a read
tool writes nothing (F-R7). With no row it answers the defaults the route
would create (every toggle on; pinned against the service by
``tests/agent/test_preference_tools.py``).
"""
from __future__ import annotations

from sqlalchemy import select

from pydantic import BaseModel, ConfigDict

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import route_tool
from app.models.notification import UserNotificationPreferences as Prefs
from app.models.user import Role
from app.routers import notifications as notifications_router

ROUTER = notifications_router.router
PATH = "/api/v1/notifications/preferences"
TABLE = Prefs.__tablename__
FIELDS = (
    "email_security", "email_account", "email_org_admin", "email_org_activity", "email_cc_statement",
    "in_app_security", "in_app_account", "in_app_org_admin", "in_app_org_activity", "in_app_cc_statement",
)


DEFAULTS = dict.fromkeys(FIELDS, True)
SECURITY_ON = {"email_security": True, "in_app_security": True}


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _read(ctx: ToolContext, _args) -> dict:
    row = await ctx.db.get(Prefs, ctx.user.id)
    # The route forces both security toggles on at read time too.
    return DEFAULTS if row is None else {**{f: getattr(row, f) for f in FIELDS}, **SECURITY_ON}


register(ToolSpec(
    name="notification_preferences_get", risk="read", args=_NoArgs, product_area=None,
    min_role=Role.MEMBER, mirrors_route=("GET", PATH), run=_read,
    description="Read this user's notification preferences: one on/off toggle per category and channel.",
))


async def _preview(ctx: ToolContext, args) -> Preview:
    if not (args.email_security and args.in_app_security):
        raise ToolError("security_emails_required", "Security notifications cannot be disabled.")
    row = await ctx.db.scalar(
        select(Prefs).where(Prefs.user_id == ctx.user.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    # No row yet: the route inserts one holding exactly these values.
    changes = [
        Change(TABLE, ctx.user.id, f, None if row is None else getattr(row, f), getattr(args, f))
        for f in FIELDS if row is None or getattr(row, f) != getattr(args, f)
    ]
    if not changes:
        raise ToolError("no_change", "the notification preferences already have these values")
    off = [c.field for c in changes if c.after is False]
    return Preview(
        summary=f"Change {len(changes)} notification preference(s)", changes=changes,
        warnings=[f"Turns off: {', '.join(off)}."] if off else [],
        # A revert would have to delete the row it created, which the tool cannot.
        context={"not_revertible": "these are the first saved preferences"} if row is None else {},
    )


register(route_tool(
    name="notification_preferences_update", risk="write",
    router=ROUTER, method="PUT", path=PATH,
    fields=FIELDS, required=FIELDS, product_area=None, min_role=Role.MEMBER,
    description=(
        "Set this user's notification preferences. Send every toggle (read them first with "
        "notification_preferences_get); email_security and in_app_security must stay true, "
        "because security notifications cannot be turned off. Returns a preview to confirm; "
        "nothing changes until it is confirmed."
    ),
    preview=_preview,
))
