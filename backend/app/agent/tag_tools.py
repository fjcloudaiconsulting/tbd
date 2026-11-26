"""Agent previews for the four tag writes. Execution uses their REST handlers."""
from inspect import unwrap

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import CascadeDisclosure, _delete_preview, _disclose, _session_factory, refusals
from app.agent.sensitive_tools import CONFIRM
from app.models.scenario import Scenario
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction
from app.models.user import Role
from app.routers import tags as routes
from app.schemas.tag import MAX_TAGS_PER_TRANSACTION, TagCreate, TagRename, TransactionTagSetReplace
from app.services import tag_service
from app.services.exceptions import NotFoundError


_COLUMNS = Tag.__table__.c


class TagIdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag_id: int


class TagRenameArgs(TagRename):
    tag_id: int


class TransactionTagsArgs(TransactionTagSetReplace):
    transaction_id: int
    tag_names: list[str] = Field(max_length=MAX_TAGS_PER_TRANSACTION)


async def _call_route(ctx: ToolContext, endpoint, **params):
    # route_limit has already drawn the shared user bucket for the limited routes.
    with refusals():
        result = await unwrap(endpoint)(
            request=None, current_user=ctx.user, db=ctx.db, session_factory=_session_factory(ctx), **params,
        )
    return jsonable_encoder(result)


async def _tags_by_name(ctx: ToolContext, names: list[str]) -> dict[str, Tag]:
    rows = await ctx.db.scalars(select(Tag).where(
        Tag.org_id == ctx.org_id, Tag.name_normalized.in_(names),
    ).order_by(Tag.name_normalized).with_for_update().execution_options(populate_existing=True))
    return {row.name_normalized: row for row in rows}


async def _ensure_local(ctx: ToolContext, **changes) -> None:
    with refusals():
        await tag_service.ensure_local_tag_changes(ctx.db, ctx.org_id, **changes)


def _tag_insert(ctx: ToolContext, group: str, name: str, normalized: str) -> list[Change]:
    return [
        Change("tags", group, "name", None, _disclose(_COLUMNS.name, name)),
        Change("tags", group, "name_normalized", None, _disclose(_COLUMNS.name_normalized, normalized)),
        Change("tags", group, "created_by_user_id", None, ctx.user.id),
    ]


async def _create_preview(ctx: ToolContext, args: TagCreate) -> Preview:
    normalized = tag_service.normalize_tag_name(args.name)
    if await _tags_by_name(ctx, [normalized]):
        raise ToolError("conflict", "A tag with that name already exists.")
    await _ensure_local(ctx, adding=[normalized])
    return Preview("Create a tag", _tag_insert(ctx, "new:1", tag_service.tag_display_name(args.name), normalized))


async def _create(ctx: ToolContext, args: TagCreate):
    return await _call_route(ctx, routes.create_tag_endpoint, body=args)


async def _rename_preview(ctx: ToolContext, args: TagRenameArgs) -> Preview:
    tag = await ctx.db.scalar(select(Tag).where(
        Tag.org_id == ctx.org_id, Tag.id == args.tag_id,
    ).with_for_update().execution_options(populate_existing=True))
    if tag is None:
        raise NotFoundError("Tag")
    name, normalized = tag_service.tag_display_name(args.name), tag_service.normalize_tag_name(args.name)
    changes = []
    if tag.name != name:
        changes.append(Change("tags", tag.id, "name", _disclose(_COLUMNS.name, tag.name), _disclose(_COLUMNS.name, name)))
    if tag.name_normalized != normalized:
        if await _tags_by_name(ctx, [normalized]):
            raise ToolError("conflict", "A tag with that name already exists.")
        await _ensure_local(ctx, withdrawing=tag.name_normalized)
        changes.append(Change("tags", tag.id, "name_normalized", _disclose(_COLUMNS.name_normalized, tag.name_normalized),
                              _disclose(_COLUMNS.name_normalized, normalized)))
    if not changes:
        raise ToolError("no_change", "The tag already has that name.")
    return Preview(f"Rename tag {tag.id}", changes)


async def _rename(ctx: ToolContext, args: TagRenameArgs):
    return await _call_route(ctx, routes.rename_tag_endpoint, tag_id=args.tag_id, body=TagRename(name=args.name))


async def _delete_check(ctx: ToolContext, tag: Tag, args: TagIdArgs) -> None:
    await _ensure_local(ctx, withdrawing=tag.name_normalized)


async def _delete_links(ctx: ToolContext, tag: Tag, args: TagIdArgs) -> list[Change]:
    tx = await ctx.db.scalars(select(TransactionTag.transaction_id).join(Transaction).where(
        TransactionTag.tag_id == tag.id, Transaction.org_id == ctx.org_id,
    ).order_by(TransactionTag.transaction_id).with_for_update())
    plans = await ctx.db.scalars(select(Scenario.id).where(
        Scenario.tag_id == tag.id, Scenario.org_id == ctx.org_id,
    ).order_by(Scenario.id).with_for_update())
    return [Change("transaction_tags", [tid, tag.id], "tag_id", tag.id, None) for tid in tx] + [
        Change("scenarios", sid, "tag_id", tag.id, None) for sid in plans
    ]


async def _delete_present(ctx: ToolContext, tag: Tag, args: TagIdArgs, changes: list[Change]) -> Preview:
    return Preview(
        f"Delete tag {tag.id}", changes, warnings=["The tag is removed from linked transactions and plans."],
        context={"deleted": [{"entity": c.entity, "id": c.id} for c in changes if c.entity == "transaction_tags"]},
    )


async def _delete(ctx: ToolContext, args: TagIdArgs):
    return await _call_route(ctx, routes.delete_tag_endpoint, tag_id=args.tag_id)


async def _replace_preview(ctx: ToolContext, args: TransactionTagsArgs) -> Preview:
    tx_id = args.transaction_id
    if await ctx.db.scalar(select(Transaction.id).where(
        Transaction.id == tx_id, Transaction.org_id == ctx.org_id,
    ).with_for_update()) is None:
        raise NotFoundError("Transaction")
    names = tag_service.replacement_tag_names(args.tag_names)
    existing = await _tags_by_name(ctx, names)
    await _ensure_local(ctx, adding=set(names) - existing.keys())
    current = (await ctx.db.scalars(select(TransactionTag.tag_id).where(
        TransactionTag.transaction_id == tx_id,
    ).order_by(TransactionTag.tag_id).with_for_update())).all()
    retained = {tag.id for tag in existing.values()}
    removed = [[tx_id, tag_id] for tag_id in current if tag_id not in retained]
    changes = [Change("transaction_tags", row_id, "tag_id", row_id[1], None) for row_id in removed]
    current_ids = set(current)
    for index, name in enumerate(names, start=1):
        tag = existing.get(name)
        if tag is None:
            tag_ref, link = f"new:tag:{index}", f"new:attachment:{index}"
            changes.extend(_tag_insert(ctx, tag_ref, name, name))
            changes.append(Change("transaction_tags", link, "transaction_id", None, tx_id))
            changes.append(Change("transaction_tags", link, "tag_id", None, tag_ref))
        elif tag.id not in current_ids:
            changes.append(Change("transaction_tags", [tx_id, tag.id], "tag_id", None, tag.id))
    if not changes:
        raise ToolError("no_change", "The transaction already has that tag set.")
    return Preview(f"Replace tags on transaction {tx_id}", changes,
                   context={"deleted": [{"entity": "transaction_tags", "id": row_id} for row_id in removed]})


async def _replace(ctx: ToolContext, args: TransactionTagsArgs):
    return await _call_route(ctx, routes.replace_transaction_tags_endpoint, transaction_id=args.transaction_id,
                             body=TransactionTagSetReplace(tag_names=args.tag_names))


register(ToolSpec(
    name="tags_create", risk="sensitive", args=TagCreate, product_area=None, min_role=Role.MEMBER,
    mirrors_route=("POST", "/api/v1/tags"), domain="transactions",
    description=f"Create an organization tag. {CONFIRM}",
    preview=_create_preview, execute=_create, route_limit=routes.create_tag_endpoint,
))
register(ToolSpec(
    name="tags_rename", risk="sensitive", args=TagRenameArgs, product_area=None, min_role=Role.MEMBER,
    mirrors_route=("PATCH", "/api/v1/tags/{tag_id}"), domain="transactions",
    description=f"Rename a tag throughout the organization, including its linked transactions and plans. {CONFIRM}",
    preview=_rename_preview, execute=_rename,
))
register(ToolSpec(
    name="tags_delete", risk="sensitive", args=TagIdArgs, product_area=None, min_role=Role.MEMBER,
    mirrors_route=("DELETE", "/api/v1/tags/{tag_id}"), domain="transactions",
    description=f"Delete an organization tag and detach it from transactions and plans. {CONFIRM}",
    preview=_delete_preview(
        "tags_delete", Tag, ("tag_id",), ("name", "name_normalized"),
        CascadeDisclosure(frozenset({"transaction_tags.tag_id", "scenarios.tag_id"}), _delete_links),
        _delete_check, _delete_present,
    ),
    execute=_delete,
))
register(ToolSpec(
    name="transactions_replace_tags", risk="sensitive", args=TransactionTagsArgs,
    product_area=None, min_role=Role.MEMBER,
    mirrors_route=("PUT", "/api/v1/transactions/{transaction_id}/tags"),
    description=f"Replace every tag on a transaction. Missing tags are created and an empty list clears them. {CONFIRM}",
    preview=_replace_preview, execute=_replace, route_limit=routes.replace_transaction_tags_endpoint,
))
