"""The report tools (TBD-669): five reads and the seven writes on
``/api/v1/reports``.

Every tool is behind the Reports product area, as the router is. The two
query reads mirror pure-read POSTs (``registry.READ_POSTS``). Their handlers
take the raw ``Request`` slowapi needs, so the tools call the same helpers
directly and draw the route's own limit, overrides included, from the bucket
REST uses (``ToolSpec.route_limit``, ``actions.route_gate``).

Visibility follows the router: a report is seen by its owner, or by the org
when it is org-shared; it is edited by its owner, or an org owner or admin
when it is org-shared. A report the caller cannot see is ``not_found``.

Every change is ``sensitive`` (browser-only confirm, never auto). Renaming a
colleague's org-shared report always needs the person; a layout, filter or
visibility change snapshots a version and may evict the oldest
(``reports_update_layout``); the rest create, delete, reset or restore.
Layouts and filters are user-shaped documents: a preview discloses one as its
canonical JSON text and a read returns every string in it, wrapped as
untrusted. Each preview names every row the confirm writes
(fenced by ``tests/agent/test_agent_honesty.py``).
"""
from __future__ import annotations


from typing import Any

from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register, untrusted_leaves
from app.agent.route_tool import CascadeDisclosure, _disclose, refusals, route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models.report import Report, ReportVersion, ReportVisibility
from app.models.user import Role
from app.routers import reports as reports_router
from app.schemas.reports_query import ReportsQuery, ReportsQueryResponse, SankeyQuery
from app.services.exceptions import NotFoundError
from app.services.feature_gate import Feature
from app.services.sankey_service import build_sankey

ROUTER = reports_router.router
REPORTS = Feature.REPORTS
VERSIONS = "report_versions"
LAYOUT = ("layout_json", "canvas_filters_json")


async def _editable(ctx: ToolContext, row: Report, _args: Any = None) -> None:
    if not reports_router._can_view(ctx.user, row):
        raise NotFoundError("Report")  # never confirm another user's private report exists
    if not reports_router._can_edit(ctx.user, row):
        raise ToolError("forbidden", "only the report's owner, or an org admin on an org-shared report, can change it")


async def _locked(ctx: ToolContext, report_id: int) -> Report:
    row = await ctx.db.scalar(
        select(Report).where(Report.id == report_id, Report.org_id == ctx.org_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("Report")
    return row


def _doc(value: Any) -> Any:
    """A layout or filter document as a preview discloses it: canonical JSON
    text, wrapped as user text (``route_tool._disclose``)."""
    return _disclose(Report.__table__.c.layout_json, value)


def _documents_untrusted(out: Any) -> Any:
    """A report read's layout and filters as untrusted subtrees: a widget id,
    a title, a filter value or a config extra is user text."""
    for report in out if isinstance(out, list) else [out]:
        for f in LAYOUT:
            report[f] = untrusted_leaves(report[f])
    return out


def _new_report(owner_id: int, version_group: str, **values: Any) -> list[Change]:
    """A report insert (``new:1``) and its original version (``version_group``)."""
    out = [Change("reports", "new:1", "owner_user_id", None, owner_id)]
    out += [Change("reports", "new:1", f, None, v) for f, v in values.items() if v is not None]
    out += [
        Change(VERSIONS, version_group, "report_id", None, "new:1"),
        Change(VERSIONS, version_group, "is_original", None, True),
        *(Change(VERSIONS, version_group, f, None, values[f]) for f in LAYOUT),
    ]
    return out


# ── reads ─────────────────────────────────────────────────────────────────

register(route_tool(
    name="reports_list", risk="read", result=_documents_untrusted,
    router=ROUTER, method="GET", path="/api/v1/reports",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description="List the saved reports this user can see (their own and the org-shared ones), newest first.",
))
register(route_tool(
    name="reports_get", risk="read", result=_documents_untrusted,
    router=ROUTER, method="GET", path="/api/v1/reports/{report_id}",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description="Read one saved report: name, visibility, widget layout and canvas filters.",
))
register(route_tool(
    name="report_versions_list", risk="read",
    router=ROUTER, method="GET", path="/api/v1/reports/{report_id}/versions",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description="List a report's saved layout versions, newest first (the original is kept).",
))


async def _query(ctx: ToolContext, args: ReportsQuery) -> dict:
    with refusals():
        rows, meta = await reports_router._run_source_query(ctx.db, args, org_id=ctx.org_id)
    out = ReportsQueryResponse(rows=rows, meta=meta).model_dump(mode="json")
    # A dimension value is a category, account or tag name: user text.
    out["rows"] = [{k: {"untrusted": v} if isinstance(v, str) else v for k, v in r.items()} for r in out["rows"]]
    return out


async def _sankey(ctx: ToolContext, args: SankeyQuery) -> dict:
    try:  # the handler's body, without its Request (slowapi) parameter
        await reports_router._resolve_relative_date_filters(ctx.db, ctx.org_id, args)
        out = await build_sankey(ctx.db, org_id=ctx.org_id, query=args)
    except ValueError as exc:
        raise ToolError("invalid_arguments", str(exc)) from None
    out = out.model_dump(mode="json")
    for link in out["links"]:  # category names
        link.update(source={"untrusted": link["source"]}, target={"untrusted": link["target"]})
    return out


register(ToolSpec(
    name="reports_query", risk="read", args=ReportsQuery, product_area=REPORTS,
    min_role=Role.MEMBER, mirrors_route=("POST", "/api/v1/reports/query"),
    description=(
        "Run a report query over the organization's data: one measure (an aggregation of a "
        "field) grouped by up to the allowed dimensions, with filters, a sort and a row limit."
    ),
    run=_query, route_limit=reports_router.run_query, loads_with_domain=True,
))
register(ToolSpec(
    name="reports_sankey_query", risk="read", args=SankeyQuery, product_area=REPORTS,
    min_role=Role.MEMBER, mirrors_route=("POST", "/api/v1/reports/query/sankey"),
    description=(
        "Cash-flow (Sankey) query: income categories into income, income into spending "
        "categories, and savings when income exceeds spending. Transfers are excluded."
    ),
    run=_sankey, route_limit=reports_router.run_sankey_query, loads_with_domain=True,
))


# ── reports_update (write) ────────────────────────────────────────────────

register(route_tool(
    name="reports_update", risk="sensitive", result=_documents_untrusted,
    router=ROUTER, method="PATCH", path="/api/v1/reports/{report_id}",
    fields=("name", "description"), nullable=("description",),
    product_area=REPORTS, min_role=Role.MEMBER,
    description=f"Rename a report or change its description (null clears it). {CONFIRM}",
    entity=Report, check=_editable,
    no_change="the report already has these values",
))


# ── reports_update_layout ─────────────────────────────────────────────────

async def _snapshot(ctx: ToolContext, row: Report, layout: dict[str, Any]) -> tuple[list[Change], list[dict]]:
    """The version ``update_report`` appends, and the oldest non-original
    versions its retention then evicts (``_snapshot_version``)."""
    kept = list((await ctx.db.scalars(
        select(ReportVersion)
        .where(ReportVersion.report_id == row.id, ReportVersion.is_original.is_(False))
        .order_by(ReportVersion.created_at.asc(), ReportVersion.id.asc())
        .execution_options(populate_existing=True)
    )).all())
    changes = [
        Change(VERSIONS, "new:1", "report_id", None, row.id),
        Change(VERSIONS, "new:1", "is_original", None, False),
        *(Change(VERSIONS, "new:1", f, None, _doc(layout[f])) for f in LAYOUT),
    ]
    evicted = kept[:max(len(kept) + 1 - reports_router.MAX_NON_ORIGINAL_VERSIONS, 0)]
    changes += [Change(VERSIONS, v.id, "report_id", v.report_id, None) for v in evicted]
    return changes, [{"entity": VERSIONS, "id": v.id} for v in evicted]


async def _layout_present(ctx: ToolContext, row: Report, args, changes: list[Change]) -> Preview:
    warnings, context = [], {"name": row.name}
    if any(c.field in LAYOUT for c in changes):
        sent = {c.field for c in changes}
        layout = {f: getattr(args, f) if f in sent else getattr(row, f) for f in LAYOUT}
        versions, deleted = await _snapshot(ctx, row, layout)
        changes = [*changes, *versions]
        if deleted:
            context["deleted"] = deleted
            warnings.append("The oldest saved version is dropped (a report keeps its original and 4 more).")
    if args.visibility == ReportVisibility.ORG and row.visibility != ReportVisibility.ORG:
        warnings.append("Everyone in the organization will see this report.")
    return Preview(
        summary=f"Change {', '.join(c.field for c in changes if c.entity == 'reports')} of report {row.id}",
        changes=changes, warnings=warnings, context=context,
    )


register(route_tool(
    name="reports_update_layout", risk="sensitive", result=_documents_untrusted,
    router=ROUTER, method="PATCH", path="/api/v1/reports/{report_id}",
    fields=("visibility", "layout_json", "canvas_filters_json"),
    args_name="ReportsUpdateLayoutArgs",
    product_area=REPORTS, min_role=Role.MEMBER,
    description=(
        "Replace a report's widget layout or canvas filters (saved as a new version), or change "
        f"its visibility (private, or org: shared with the organization). {CONFIRM}"
    ),
    entity=Report, check=_editable, present=_layout_present,
    no_change="the report already has this layout",
))


# ── reports_create ────────────────────────────────────────────────────────

async def _create_present(ctx: ToolContext, _row, args, changes: list[Change]) -> Preview:
    values = {c.field: c.after for c in changes}
    return Preview(
        summary="Create a report",
        changes=_new_report(ctx.user.id, "new:2", **values),
        context={"name": args.name},
    )


register(route_tool(
    name="reports_create", risk="sensitive", result=_documents_untrusted,
    router=ROUTER, method="POST", path="/api/v1/reports",
    fields=("name", "description", "visibility", "layout_json", "canvas_filters_json"),
    required=("name",), product_area=REPORTS, min_role=Role.MEMBER,
    description=(
        "Create a saved report (private unless visibility is org), optionally with a widget "
        f"layout and canvas filters; an empty layout is a blank report. {CONFIRM}"
    ),
    entity=Report, present=_create_present,
))


# ── reports_duplicate ─────────────────────────────────────────────────────

async def _duplicate_preview(ctx: ToolContext, args) -> Preview:
    source = await ctx.db.scalar(
        select(Report).where(Report.id == args.report_id, Report.org_id == ctx.org_id)
        .execution_options(populate_existing=True)
    )
    if source is None or not reports_router._can_view(ctx.user, source):
        raise NotFoundError("Report")
    name = f"{source.name} (copy)"
    return Preview(
        summary=f"Copy report {source.id} as a new private report",
        changes=_new_report(
            ctx.user.id, "new:2", visibility=ReportVisibility.PRIVATE.value, name={"untrusted": name},
            description=None if source.description is None else {"untrusted": source.description},
            layout_json=_doc(source.layout_json), canvas_filters_json=_doc(source.canvas_filters_json),
        ),
        context={"name": name},
    )


register(route_tool(
    name="reports_duplicate", risk="sensitive", result=_documents_untrusted,
    router=ROUTER, method="POST", path="/api/v1/reports/{report_id}/duplicate",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description=f"Copy a report the user can see into a new private report named '<name> (copy)'. {CONFIRM}",
    preview=_duplicate_preview,
))


# ── reports_reset / report_version_restore ────────────────────────────────

def _restore(row: Report, version: ReportVersion | None, what: str) -> Preview:
    """``_restore_version``: the version's layout and filters onto the report."""
    changes = [] if version is None else [
        Change("reports", row.id, f, _doc(getattr(row, f)), _doc(getattr(version, f)))
        for f in LAYOUT if getattr(row, f) != getattr(version, f)
    ]
    if not changes:
        raise ToolError("no_change", f"the report already shows {what}")
    return Preview(
        summary=f"Restore {what} of report {row.id}", changes=changes, context={"name": row.name},
    )


async def _reset_preview(ctx: ToolContext, args) -> Preview:
    row = await _locked(ctx, args.report_id)
    await _editable(ctx, row)
    original = await ctx.db.scalar(
        select(ReportVersion)
        .where(ReportVersion.report_id == row.id, ReportVersion.is_original.is_(True))
        .order_by(ReportVersion.id.asc()).limit(1).execution_options(populate_existing=True)
    )
    return _restore(row, original, "the original layout")


async def _version_preview(ctx: ToolContext, args) -> Preview:
    row = await _locked(ctx, args.report_id)
    await _editable(ctx, row)
    version = await ctx.db.scalar(
        select(ReportVersion).where(ReportVersion.id == args.version_id)
        .execution_options(populate_existing=True)
    )
    if version is None or version.report_id != row.id:
        raise NotFoundError("Version")
    return _restore(row, version, f"version {version.id}")


register(route_tool(
    name="reports_reset", risk="sensitive", result=_documents_untrusted,
    router=ROUTER, method="POST", path="/api/v1/reports/{report_id}/reset",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description=f"Put a report's layout and canvas filters back to how it was created. {CONFIRM}",
    preview=_reset_preview,
))
register(route_tool(
    name="report_version_restore", risk="sensitive", result=_documents_untrusted,
    router=ROUTER, method="POST", path="/api/v1/reports/{report_id}/versions/{version_id}/restore",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description=(
        "Put a report's layout and canvas filters back to a saved version "
        f"(report_versions_list). {CONFIRM}"
    ),
    preview=_version_preview,
))


# ── reports_delete ────────────────────────────────────────────────────────

async def _versions_go(ctx: ToolContext, row: Report, args) -> list[Change]:
    ids = (await ctx.db.scalars(
        select(ReportVersion.id).where(ReportVersion.report_id == row.id).order_by(ReportVersion.id)
    )).all()
    return [Change(VERSIONS, i, "report_id", row.id, None) for i in ids]


async def _delete_present(ctx: ToolContext, row: Report, args, changes: list[Change]) -> Preview:
    return Preview(
        summary=f"Delete report {row.id} and its saved versions", changes=changes,
        warnings=["A delete cannot be undone."],
        context={"name": row.name, "deleted": [
            {"entity": "reports", "id": row.id},
            *({"entity": c.entity, "id": c.id} for c in changes if c.entity == VERSIONS),
        ]},
    )


register(route_tool(
    name="reports_delete", risk="sensitive",
    router=ROUTER, method="DELETE", path="/api/v1/reports/{report_id}",
    fields=(), product_area=REPORTS, min_role=Role.MEMBER,
    description=f"Delete a saved report and its version history. Its data is not touched. {CONFIRM}",
    entity=Report, describe=("name", "visibility"), check=_editable, present=_delete_present,
    cascade=CascadeDisclosure(covers=frozenset({"report_versions.report_id"}), disclose=_versions_go),
))
