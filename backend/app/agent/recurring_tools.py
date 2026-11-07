"""The recurring-template tools (TBD-667): ``recurring_list`` and the seven
writes on ``/api/v1/recurring``.

Each is route-bound (``app.agent.route_tool``): execute runs the REST handler.
Each preview names every row the confirm changes (fenced by
``tests/agent/test_agent_honesty.py``): the pending occurrences an edit of the
template mirrors onto, the occurrences a stop or delete removes with their
tags, the rows those deletions unlink or mark rejected, the import-batch
counters, the rows that leave the series, and every occurrence generation
writes with the balances it moves. A transfer template (TBD-600) writes a
linked pair per occurrence, an expense leg on ``account_id`` and an income leg
on ``to_account_id``: every preview names both legs, both balances, and the
pending pairs an edit moves; a removal never splits a pair.

``recurring_update`` is the one ``write``: description, category and pause.
It changes the template and pending occurrences only, and nothing it sets
moves money later either; its inverse restores the template. Everything else
is ``sensitive`` (never auto-applied, confirmed only by the person in the
app): ``recurring_update_money`` sets what generation later settles into the
balance (amount, account, type, auto-settle, schedule, resume, count), as a
pending amount edit (``transactions_update_money``) is; generation and
materialising move or create money rows; a create, a skip and a materialise
insert rows no revert can remove; a stop or delete removes rows and marks
matched import rows rejected for good.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import exists, func, select
from sqlalchemy.orm import aliased

from app.agent.registry import Change, Preview, ToolContext, ToolError, register
from app.agent.route_tool import CascadeDisclosure, route_tool
from app.agent.sensitive_tools import (
    CONFIRM, MONEY, _add, _balance_changes, _batch_changes, _currencies, _deleted, _iso, _signed,
)
from app.models import Organization, Transaction
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import PlanGoal
from app.models.tag import TransactionTag
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role
from app.routers import recurring as recurring_router
from app.services import recurring_service
from app.services.billing_service import current_cycle_window
from app.services.date_utils import advance_date
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.recurring_filters import active_series_filter, has_remaining_occurrences
from app.services.transaction_filters import (
    REVERTED_RECONCILIATION_STATES, is_reciprocal_pair, non_reverted_transaction_filter,
)
from app.services.transaction_service import (
    _series_sync_targets, validate_account, validate_category_for_type,
)

ROUTER = recurring_router.router
TEMPLATE = "recurring_transactions"
PENDING = TransactionStatus.PENDING.value
TRANSFER = "transfer"


def _accounts(r) -> set[int]:
    """The accounts a template's occurrences land on: a transfer's two (TBD-600)."""
    return {r.account_id, r.to_account_id} - {None}


async def _template(ctx: ToolContext, recurring_id: int, *, lock: bool) -> RecurringTransaction:
    q = select(RecurringTransaction).where(
        RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == ctx.org_id
    ).execution_options(populate_existing=True)
    r = await ctx.db.scalar(q.with_for_update() if lock else q)
    if r is None:
        raise NotFoundError("Recurring transaction")
    return r


def _conflict(detail: str) -> ToolError:
    return ToolError("conflict", detail)


register(route_tool(
    name="recurring_list", risk="read",
    router=ROUTER, method="GET", path="/api/v1/recurring",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description="List the organization's recurring transaction templates and their schedules.",
))


# ── recurring_create ──────────────────────────────────────────────────────

async def _create_check(ctx: ToolContext, _row, args) -> None:
    await validate_account(ctx.db, args.account_id, ctx.org_id)
    await validate_category_for_type(ctx.db, args.category_id, ctx.org_id, TransactionType(args.type))
    await recurring_service._validate_transfer_accounts(
        ctx.db, ctx.org_id, args.type, args.account_id, args.to_account_id,
    )
    await recurring_service.validate_frontier(ctx.db, ctx.org_id, args.next_due_date)


async def _create_present(ctx: ToolContext, _row, args, changes: list[Change]) -> Preview:
    cur = (await _currencies(ctx, {args.account_id})).get(args.account_id)  # both legs share it
    return Preview(
        summary=f"Create a recurring {args.type}",
        changes=[Change(c.entity, c.id, c.field, c.before, c.after, cur if c.field == "amount" else None)
                 for c in changes],
        context={"description": args.description},
    )


register(route_tool(
    name="recurring_create", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/recurring",
    fields=("account_id", "to_account_id", "category_id", "description", "amount", "type",
            "frequency", "next_due_date", "auto_settle", "occurrence_count"),
    required=("account_id", "category_id", "description", "amount", "type", "frequency",
              "next_due_date"),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Create a recurring income, expense or transfer template. Generation then writes each "
        "occurrence from next_due_date on (settled ones move the balance when auto_settle is "
        "on); occurrence_count limits how many. A transfer moves money from account_id to "
        "to_account_id (another account in the same currency, not a credit card or loan) as a "
        "linked expense and income pair, and needs a category of type both. "
        f"{CONFIRM}"
    ),
    entity=RecurringTransaction, check=_create_check, present=_create_present,
))


# ── recurring_update / recurring_update_money ─────────────────────────────
#
# One route, two tools, by what the field does once generation runs (TBD-667
# review): an ``agent:auto`` write could otherwise raise the amount and turn
# on auto-settle, and the scheduled generation would settle it into the
# balance with no person ever confirming. Each tool refuses the other's
# fields (its args model forbids them) and the other's ``is_active`` value:
# the write pauses, only the sensitive tool resumes. The category is on both:
# a type change has to carry a category valid for the new type.
#
# ``update_recurring`` writes beyond the fields sent, and the preview names it:
# a resume (``is_active`` false -> true, or a higher ``occurrence_count`` on a
# finished series) walks ``next_due_date`` onto the current billing cycle; an
# amount, description, category or account change follows onto every pending
# occurrence (``_series_sync_targets``) that still holds the old value. The
# validators and the re-anchor are the service's own, run on a detached copy
# of the template so the preview writes nothing.

UPDATE_FIELDS = ("account_id", "to_account_id", "category_id", "description", "amount", "type",
                 "frequency", "next_due_date", "auto_settle", "is_active", "occurrence_count")


async def _pair_changes(ctx: ToolContext, r: RecurringTransaction, new: dict) -> tuple[list[Change], int]:
    """What ``_sync_pending_transfer_pairs`` writes onto a transfer's pending
    pairs (TBD-600): its legs, read as it reads them, and its rules applied
    to a copy. A field follows only while it still holds the old template
    value; the source account onto the expense leg, the destination onto the
    income leg, never onto the partner's account. Returns the Changes and how
    many pairs (occurrences) they touch."""
    referrer = aliased(Transaction)
    legs = (await ctx.db.scalars(
        select(Transaction).where(
            Transaction.org_id == ctx.org_id, Transaction.recurring_id == r.id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.linked_transaction_id.is_not(None),
            Transaction.reconciliation_state.not_in(REVERTED_RECONCILIATION_STATES),
            ~exists().where(
                referrer.org_id == ctx.org_id, referrer.linked_transaction_id == Transaction.id,
                referrer.id != Transaction.linked_transaction_id,
            ),
        ).order_by(Transaction.id).with_for_update().execution_options(populate_existing=True)
    )).all()
    by_id = {t.id: t for t in legs}
    cur = await _currencies(ctx, _accounts(r) | _accounts(SimpleNamespace(**new)))
    out: list[Change] = []
    pairs = 0
    for exp in legs:
        inc = by_id.get(exp.linked_transaction_id)
        if exp.type != TransactionType.EXPENSE or not is_reciprocal_pair(exp, inc):
            continue
        before = len(out)
        after = {leg.id: {"amount": leg.amount, "category_id": leg.category_id,
                          "description": leg.description, "account_id": leg.account_id}
                 for leg in (exp, inc)}
        if exp.amount == inc.amount == r.amount:
            after[exp.id]["amount"] = after[inc.id]["amount"] = new["amount"]
        if exp.category_id == inc.category_id == r.category_id:
            after[exp.id]["category_id"] = after[inc.id]["category_id"] = new["category_id"]
        for leg in (exp, inc):
            if leg.description == r.description:
                after[leg.id]["description"] = new["description"]
        if exp.account_id == r.account_id and new["account_id"] != inc.account_id:
            after[exp.id]["account_id"] = new["account_id"]
        if inc.account_id == r.to_account_id and new["to_account_id"] != after[exp.id]["account_id"]:
            after[inc.id]["account_id"] = new["to_account_id"]
        for leg in (exp, inc):
            for f, a in after[leg.id].items():
                b = getattr(leg, f)
                if a == b:
                    continue
                if f == "amount":
                    out.append(Change("transactions", leg.id, f, f"{b:.2f}", f"{a:.2f}",
                                      cur.get(after[leg.id]["account_id"])))
                elif f == "description":
                    out.append(Change("transactions", leg.id, f, {"untrusted": b}, {"untrusted": a}))
                else:
                    out.append(Change("transactions", leg.id, f, b, a))
        pairs += len(out) > before
    return out, pairs


async def _update_present(ctx: ToolContext, r: RecurringTransaction, args, changes: list[Change]) -> Preview:
    sent = args.model_fields_set
    new = {f: getattr(args, f) if f in sent else getattr(r, f) for f in UPDATE_FIELDS}
    if "account_id" in sent:
        await validate_account(ctx.db, new["account_id"], ctx.org_id)
    if "account_id" in sent or "to_account_id" in sent:
        await recurring_service._validate_transfer_accounts(
            ctx.db, ctx.org_id, r.type, new["account_id"], new["to_account_id"],
        )
    if "type" in sent and new["type"] != r.type and r.type == TRANSFER:
        raise ValidationError("A recurring transfer cannot change type. Stop it and create a new series.")
    if "type" in sent or "category_id" in sent:
        await validate_category_for_type(ctx.db, new["category_id"], ctx.org_id, TransactionType(new["type"]))
    today = datetime.date.today()
    post = SimpleNamespace(
        id=r.id, next_due_date=new["next_due_date"], frequency=Frequency(new["frequency"]),
        occurrence_count=new["occurrence_count"], occurrences_elapsed=r.occurrences_elapsed,
        last_spent_date=r.last_spent_date,
    )
    resumed = getattr(args, "is_active", None) is True and not r.is_active
    if resumed or (has_remaining_occurrences(post) and not has_remaining_occurrences(r)):
        await recurring_service._reanchor_frontier_on_resume(ctx.db, ctx.org_id, post, today=today)
    try:
        if "next_due_date" in sent or "frequency" in sent:
            await recurring_service.validate_frontier(ctx.db, ctx.org_id, post.next_due_date, today=today)
        if "next_due_date" in sent and post.next_due_date != r.next_due_date:
            await recurring_service._check_frontier_clears_series(
                ctx.db, ctx.org_id, post, old_next_due=r.next_due_date,
            )
    except ConflictError as exc:
        raise _conflict(exc.detail) from None

    acct_after = new["account_id"]
    targets_all = await _series_sync_targets(ctx.db, ctx.org_id, r.id, None)
    cur = await _currencies(ctx, {r.account_id, acct_after} | {t.account_id for t in targets_all})
    out = [
        Change(c.entity, c.id, c.field, c.before, c.after, cur.get(acct_after) if c.field == "amount" else None)
        for c in changes if c.field != "next_due_date"
    ]
    if post.next_due_date != r.next_due_date:
        out.append(Change(TEMPLATE, r.id, "next_due_date", _iso(r.next_due_date), _iso(post.next_due_date)))

    def moved_to(t: Transaction) -> int:
        return acct_after if acct_after != r.account_id and t.account_id == r.account_id else t.account_id

    old_type = TransactionType(r.type)
    siblings: list[Change] = []
    if new["amount"] != r.amount:
        for t in await _series_sync_targets(ctx.db, ctx.org_id, r.id, old_type):
            if t.amount == r.amount:
                siblings.append(Change("transactions", t.id, "amount", f"{r.amount:.2f}",
                                       f"{new['amount']:.2f}", cur.get(moved_to(t))))
    same_type = TransactionType(new["type"]) == old_type
    guarded = [
        (f, only) for f, only in (("description", None), ("category_id", old_type), ("account_id", None))
        if new[f] != getattr(r, f) and (f != "category_id" or same_type)
    ]
    for t in targets_all:
        for f, only in guarded:
            if getattr(t, f) == getattr(r, f) and (only is None or t.type == only):
                before, after = getattr(r, f), new[f]
                if f == "description":
                    before, after = {"untrusted": before}, {"untrusted": after}
                siblings.append(Change("transactions", t.id, f, before, after))
    occurrences = len({c.id for c in siblings})
    if r.type == TRANSFER:  # its legs are linked, so the sync above skips them
        pair_changes, pairs = await _pair_changes(ctx, r, new)
        siblings += pair_changes
        occurrences += pairs
    siblings.sort(key=lambda c: c.id)
    warnings = []
    if siblings:
        warnings.append(f"Also updates {occurrences} pending occurrence(s) that still had the old value.")
    if post.next_due_date != r.next_due_date and "next_due_date" not in sent:
        warnings.append("Resuming moves the next due date onto the current billing cycle.")
    return Preview(
        summary=f"Change {', '.join(dict.fromkeys(c.field for c in out))} of recurring transaction {r.id}",
        changes=out + siblings, warnings=warnings, context={"description": r.description},
    )


def _is_active_only(allowed: bool, other: str):
    async def check(ctx: ToolContext, r: RecurringTransaction, args) -> None:
        if "is_active" in args.model_fields_set and args.is_active is not allowed:
            verb = "pause" if allowed else "resume"
            raise ToolError("invalid_arguments", f"this tool cannot {verb} a template; use {other}")
    return check


register(route_tool(
    name="recurring_update", risk="write",
    router=ROUTER, method="PUT", path="/api/v1/recurring/{recurring_id}",
    fields=("description", "category_id", "is_active"),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Rename a recurring template, change its category, or pause it (is_active false). A "
        "description or category change also applies to its pending occurrences that still "
        "have the old value (both legs of a transfer's pending pairs); a transfer's category "
        "must be of type both. Use recurring_update_money for the amount, account, type, "
        "auto-settle, schedule, count, or to resume. Returns a preview to confirm; nothing "
        "changes until it is confirmed."
    ),
    entity=RecurringTransaction, check=_is_active_only(False, "recurring_update_money"),
    present=_update_present, no_change="the recurring transaction already has these values",
))
register(route_tool(
    name="recurring_update_money", risk="sensitive",
    router=ROUTER, method="PUT", path="/api/v1/recurring/{recurring_id}",
    fields=("amount", "account_id", "to_account_id", "type", "category_id", "auto_settle",
            "frequency", "next_due_date", "occurrence_count", "is_active"),
    nullable=("occurrence_count",), product_area=None, min_role=Role.MEMBER,
    description=(
        "Change what a recurring template generates: its amount, account, a transfer's "
        "destination (to_account_id), type (income or expense, with a category valid for it; a "
        "transfer cannot change type and its category must be of type both), auto_settle, frequency, next_due_date or occurrence_count "
        "(null: open-ended), or resume it (is_active true). An amount or account change also "
        "applies to its pending occurrences (a transfer's pending pairs) that still have the old "
        f"value; generation settles auto-settle occurrences into the balance. {CONFIRM}"
    ),
    entity=RecurringTransaction, check=_is_active_only(True, "recurring_update"),
    present=_update_present, no_change="the recurring transaction already has these values",
))


# ── recurring_stop / recurring_delete ─────────────────────────────────────
#
# Both remove the template's pending occurrences from today on through
# ``plan_pending_removal``, the decision the removal applies, then detach the
# rows that survive: stop NULLs ``recurring_id``, delete lets the foreign key
# (``transactions.recurring_id``, SET NULL) do it.

async def _removal(ctx: ToolContext, r: RecurringTransaction) -> list[Change]:
    plan = await recurring_service.plan_pending_removal(ctx.db, ctx.org_id, r.id)
    out: list[Change] = []
    for rid in sorted(plan.doomed):
        out += _deleted(plan.rows[rid])
    tags = (await ctx.db.execute(
        select(TransactionTag.transaction_id, TransactionTag.tag_id)
        .where(TransactionTag.transaction_id.in_(sorted(plan.doomed)))
        .order_by(TransactionTag.transaction_id, TransactionTag.tag_id)
    )).all() if plan.doomed else []
    out += [Change("transaction_tags", [t, g], "tag_id", g, None) for t, g in tags]
    prior = plan.settlement.prior_states if plan.settlement else {}
    for row in sorted(plan.rows.values(), key=lambda x: x.id):
        if row.id in plan.doomed or row.linked_transaction_id not in plan.doomed:
            continue
        if row.id in prior:
            out.append(Change("transactions", row.id, "reconciliation_state", prior[row.id], "rejected"))
        out.append(Change("transactions", row.id, "linked_transaction_id", row.linked_transaction_id, None))
    if plan.settlement:
        out += await _batch_changes(ctx, plan)
    survivors = (await ctx.db.scalars(
        select(Transaction.id).where(
            Transaction.org_id == ctx.org_id, Transaction.recurring_id == r.id,
            Transaction.id.notin_(sorted(plan.doomed)),
        ).order_by(Transaction.id)
    )).all()
    out += [Change("transactions", sid, "recurring_id", r.id, None) for sid in survivors]
    return out


async def _removal_preview(
    ctx: ToolContext, summary: str, r: RecurringTransaction, changes: list[Change], deleted: list,
) -> Preview:
    gone = {c.id for c in changes if c.entity == "transactions" and c.field == "type"}
    demoted = sum(1 for c in changes if c.field == "reconciliation_state")
    warnings = []
    if gone:
        links = dict((await ctx.db.execute(
            select(Transaction.id, Transaction.linked_transaction_id).where(Transaction.id.in_(sorted(gone)))
        )).all())
        occurrences = len({min(i, links[i]) if links[i] in gone else i for i in gone})
        warnings.append(f"{occurrences} pending occurrence(s) are deleted.")
    # A due-later pending row the removal keeps is a transfer leg whose
    # partner is settled or past-dated: a pair is never split (TBD-600).
    kept = await ctx.db.scalar(select(func.count(Transaction.id)).where(
        Transaction.org_id == ctx.org_id, Transaction.recurring_id == r.id,
        Transaction.status == TransactionStatus.PENDING, Transaction.date >= datetime.date.today(),
        Transaction.id.notin_(sorted(gone)),
    ))
    if kept:
        warnings.append(
            f"{kept} pending transfer leg(s) are kept, because the other leg of the pair is "
            "already settled or past: a transfer pair is never split."
        )
    if demoted:
        warnings.append(
            f"{demoted} matched import row(s) pointing at them will be marked rejected, "
            "which cannot be undone."
        )
    if any(c.field == "recurring_id" for c in changes):
        warnings.append("The other transactions of the series are kept but leave the series.")
    deleted += [{"entity": "transactions", "id": rid} for rid in sorted(gone)] + [
        {"entity": "transaction_tags", "id": c.id} for c in changes if c.entity == "transaction_tags"
    ]
    return Preview(summary=summary, changes=changes, warnings=warnings,
                   context={"description": r.description, "deleted": deleted})


async def _stop_preview(ctx: ToolContext, args) -> Preview:
    r = await _template(ctx, args.recurring_id, lock=False)
    changes = [Change(TEMPLATE, r.id, "is_active", True, False)] if r.is_active else []
    changes += await _removal(ctx, r)
    if not changes:
        raise ToolError("no_change", "the recurring transaction is already stopped")
    return await _removal_preview(ctx, f"Stop recurring transaction {r.id}", r, changes, [])


register(route_tool(
    name="recurring_stop", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/recurring/{recurring_id}/stop",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "Stop a recurring template: it generates nothing more, its pending occurrences from "
        "today on are deleted, matched import rows pointing at them are marked rejected "
        "(reported as demoted_ids), and its other transactions leave the series. "
        f"{CONFIRM}"
    ),
    preview=_stop_preview,
))


async def _delete_cascade(ctx: ToolContext, r: RecurringTransaction, args) -> list[Change]:
    goals = await ctx.db.scalars(
        select(PlanGoal.id).where(PlanGoal.funding_recurring_id == r.id).order_by(PlanGoal.id)
    )
    # A plan goal funded by the series loses that link (SET NULL).
    return await _removal(ctx, r) + [Change("plan_goals", g, "funding_recurring_id", r.id, None) for g in goals]


async def _delete_present(ctx: ToolContext, r: RecurringTransaction, args, changes: list[Change]) -> Preview:
    cur = (await _currencies(ctx, {r.account_id})).get(r.account_id)
    changes = [
        Change(c.entity, c.id, c.field, c.before, c.after, cur)
        if (c.entity, c.field) == (TEMPLATE, "amount") else c for c in changes
    ]
    pv = await _removal_preview(ctx, f"Delete recurring transaction {r.id}", r, changes, [])
    pv.warnings.insert(0, "A delete cannot be undone.")
    return pv


register(route_tool(
    name="recurring_delete", risk="sensitive",
    router=ROUTER, method="DELETE", path="/api/v1/recurring/{recurring_id}",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "Delete a recurring template. Its pending occurrences from today on are deleted, "
        "matched import rows pointing at them are marked rejected (reported as demoted_ids), "
        "and its other transactions are kept outside any series. Cannot be undone. "
        f"{CONFIRM}"
    ),
    entity=RecurringTransaction,
    describe=("description", "amount", "type", "frequency", "next_due_date", "account_id"),
    present=_delete_present,
    cascade=CascadeDisclosure(
        covers=frozenset({"transactions.recurring_id", "plan_goals.funding_recurring_id"}),
        disclose=_delete_cascade,
    ),
))


# ── recurring_skip_next / recurring_materialise_next ──────────────────────
#
# ``materialise_next`` writes the next occurrence now (always PENDING, so no
# balance moves) and spends it. The preview refuses what it refuses, read
# under the same template lock.

def _legs(r) -> list[tuple[int, str]]:
    """(account, type) of each row one occurrence writes: a transfer is an
    expense leg on the source and an income leg on the destination."""
    if r.type == TRANSFER:
        return [(r.account_id, "expense"), (r.to_account_id, "income")]
    return [(r.account_id, r.type)]


def _occurrence(
    r: RecurringTransaction, due: datetime.date, status: str, cur: dict, first: int = 1, **extra,
) -> list[Change]:
    """The rows ``_create_occurrence`` inserts for ``r`` at ``due``, as groups
    ``new:first`` (and ``new:first+1``, the income leg of a transfer, the two
    linked to each other)."""
    legs = _legs(r)
    out: list[Change] = []
    for i, (account_id, leg_type) in enumerate(legs):
        gid, other = f"new:{first + i}", f"new:{first + 1 - i}"
        cols = {
            "account_id": account_id, "category_id": r.category_id,
            "description": {"untrusted": r.description}, "amount": f"{r.amount:.2f}", "type": leg_type,
            "status": status, "date": _iso(due),
            "settled_date": _iso(due) if status == TransactionStatus.SETTLED.value else None,
            "recurring_id": r.id, "linked_transaction_id": other if len(legs) == 2 else None, **extra,
        }
        out += [Change("transactions", gid, f, None, v, cur.get(account_id) if f == "amount" else None)
                for f, v in cols.items() if v is not None]
    return out


def _next_preview(skipped: bool):
    async def _preview(ctx: ToolContext, args) -> Preview:
        r = await _template(ctx, args.recurring_id, lock=True)
        due = r.next_due_date
        if due != args.occurrence_date:
            raise _conflict(f"The next occurrence is {due.isoformat()}, not {args.occurrence_date.isoformat()}.")
        if not r.is_active:
            raise _conflict("This recurring transaction is stopped.")
        if not has_remaining_occurrences(r):
            raise _conflict("This recurring transaction has no occurrences left.")
        if due < await recurring_service.frontier_lower_bound(ctx.db, ctx.org_id):
            raise _conflict(f"The next occurrence ({due.isoformat()}) is before the current billing cycle.")
        if await ctx.db.scalar(select(Transaction.id).where(
            Transaction.org_id == ctx.org_id, Transaction.recurring_id == r.id, Transaction.date == due,
        ).limit(1)):
            raise _conflict(f"The occurrence on {due.isoformat()} already exists.")
        cur = await _currencies(ctx, _accounts(r))
        extra = {"reconciliation_state": "skipped"} if skipped else {}
        changes = _occurrence(r, due, PENDING, cur, **extra) + [
            Change(TEMPLATE, r.id, "next_due_date", _iso(due), _iso(advance_date(due, r.frequency))),
            Change(TEMPLATE, r.id, "occurrences_elapsed", r.occurrences_elapsed, r.occurrences_elapsed + 1),
            Change(TEMPLATE, r.id, "last_spent_date", _iso(r.last_spent_date), _iso(due)),
        ]
        verb = "Skip" if skipped else "Write"
        return Preview(
            summary=f"{verb} the {due.isoformat()} occurrence of recurring transaction {r.id}",
            changes=changes, context={"description": r.description},
        )
    return _preview


register(route_tool(
    name="recurring_skip_next", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/recurring/{recurring_id}/skip-next",
    fields=("occurrence_date",), required=("occurrence_date",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Skip the next occurrence of a recurring template: it is written as a skipped pending "
        "transaction (counted nowhere) and the schedule moves on. occurrence_date must be the "
        f"template's next_due_date. Cannot be undone. {CONFIRM}"
    ),
    preview=_next_preview(skipped=True),
))
register(route_tool(
    name="recurring_materialise_next", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/recurring/{recurring_id}/materialise-next",
    fields=("occurrence_date",), required=("occurrence_date",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Write the next occurrence of a recurring template now, as a pending transaction whose "
        "amount can then be edited; the schedule moves on. occurrence_date must be the "
        f"template's next_due_date. {CONFIRM}"
    ),
    preview=_next_preview(skipped=False),
))


# ── recurring_generate ────────────────────────────────────────────────────
#
# ``generate_due_transactions``, read-only: first the PENDING rows of
# auto-settle templates whose expected settlement has come settle
# (``recurring_service.due_auto_settle_units``, the sweep's own selection),
# then every active, unfinished template due by the end of the current cycle
# walks its schedule, writing each occurrence that has no row yet (settled,
# with its balance, when auto-settle and due) and spending each one.

async def _generate_preview(ctx: ToolContext, args) -> Preview:
    db, org_id, today = ctx.db, ctx.org_id, datetime.date.today()
    cycle_day = await db.scalar(select(Organization.billing_cycle_day).where(Organization.id == org_id))
    _, period_end = current_cycle_window(cycle_day or 1, today)
    changes: list[Change] = []
    deltas: dict[int, Decimal] = {}
    units = await recurring_service.due_auto_settle_units(db, org_id, today, lock=False)
    settling = len(units)  # occurrences: both legs of a due transfer pair count once
    for tx in (tx for unit in units for tx in unit):
        changes.append(Change("transactions", tx.id, "status", PENDING, "settled"))
        if tx.settled_date is None:  # settles on its expected date, else its date
            changes.append(Change("transactions", tx.id, "settled_date", None, _iso(tx.date)))
        _add(deltas, tx.account_id, _signed(tx.amount, tx.type))

    templates = (await db.scalars(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id, active_series_filter(),
            RecurringTransaction.next_due_date <= period_end,
        ).order_by(RecurringTransaction.id).execution_options(populate_existing=True)
    )).all()
    cur = await _currencies(ctx, {a for r in templates for a in _accounts(r)})
    k = n = 0  # insert groups, occurrences
    for r in templates:
        walk = SimpleNamespace(occurrence_count=r.occurrence_count, occurrences_elapsed=r.occurrences_elapsed)
        due, steps = r.next_due_date, 0
        while due <= period_end and has_remaining_occurrences(walk) and steps < recurring_service.MAX_CATCHUP_ITERATIONS:
            steps += 1
            exists = await db.scalar(select(Transaction.id).where(
                Transaction.org_id == org_id, Transaction.recurring_id == r.id, Transaction.date == due,
            ).limit(1))
            if not exists:
                n += 1
                settled = r.auto_settle and due <= today
                status = "settled" if settled else PENDING
                changes += _occurrence(r, due, status, cur, first=k + 1)
                k += len(_legs(r))
                if settled:
                    for account_id, leg_type in _legs(r):
                        _add(deltas, account_id, _signed(r.amount, leg_type))
            spent, due = due, advance_date(due, r.frequency)
            walk.occurrences_elapsed += 1
        if due != r.next_due_date:
            changes += [
                Change(TEMPLATE, r.id, "next_due_date", _iso(r.next_due_date), _iso(due)),
                Change(TEMPLATE, r.id, "occurrences_elapsed", r.occurrences_elapsed, walk.occurrences_elapsed),
                Change(TEMPLATE, r.id, "last_spent_date", _iso(r.last_spent_date), _iso(spent)),
            ]
    money = await _balance_changes(ctx, deltas)
    if not changes:
        raise ToolError("no_change", "no recurring transaction is due")
    return Preview(
        summary=f"Write {n} recurring occurrence(s) due by {period_end.isoformat()} "
                f"and settle {settling} due one(s)",
        changes=changes + money, warnings=[MONEY] if money else [],
    )


register(route_tool(
    name="recurring_generate", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/recurring/generate",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "Write every recurring occurrence due by the end of the current billing cycle, and settle "
        "the pending ones of auto-settle templates whose date has passed. Settled rows move "
        f"account balances. {CONFIRM}"
    ),
    preview=_generate_preview,
))
