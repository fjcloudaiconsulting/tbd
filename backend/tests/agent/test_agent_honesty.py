"""TBD-664 PR A0: the honesty fence over every agent write tool.

A preview is a promise: what a staged action says it will change must be what
confirm changes, column by column, and nothing else. The fence snapshots every
table of the SQLite world (FK enforcement ON, so CASCADE and SET NULL rows are
visible) around EVERY preview call and around execute, and checks, both ways:

1. preview window: zero diff (a preview writes nothing, not even an unflushed
   ``db.add`` that only propose's commit would persist);
2. diff -> Change: every changed column / inserted row / deleted row is named;
3. Change -> diff: every named Change happened, with the named before/after
   (or, for ``*_delta`` counters, the named nonzero movement);
4. inserts: one-to-one with insert groups, every named column equals the row,
   every unnamed column is exempt or the column default;
5. deletes: one-to-one with delete groups, named befores equal the row;
6. identity and value normalisation;
7. pinned exemption lists (bookkeeping, insert identity);
8. tier: a ``write`` tool never moves or names ``accounts.balance``.

Fences: T-A9 T-A9b T-A10 T-A10b. Guards: T-A9c. Every mutant below names the
wrong implementation it kills.
"""
from __future__ import annotations

import datetime
import enum
import json
from dataclasses import dataclass
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from app.agent import registry
from app.agent.registry import Change, Preview, ToolSpec
from app.models import Account
from app.models.agent_pending_action import AgentPendingAction
from app.models.base import Base
from app.models.budget import Budget
from app.models.category_rule import CategoryRule, RuleSource
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role
from app.services.category_rules_service import normalize_description
from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    AUTO, P_START, _confirm, _invoke, engine, factory, w,
)

# Counters are named only as signed nonzero deltas, never as absolutes.
DELTA_COLUMNS = {
    ("accounts", "balance"),
    ("import_batches", "row_count"),
    ("import_batches", "accepted_count"),
    ("import_batches", "pending_count"),
}
# Bookkeeping: never named, never required.
BOOKKEEPING_COLS = {"updated_at", "created_at"}
BOOKKEEPING = {
    ("category_rules", "match_count"),
    ("category_rules", "raw_description_seen"),
    ("category_rules", "source"),
}
BOOKKEEPING_TABLES = {"audit_events"}
# Insert identity beyond the PK and org_id: columns a natural-key group id names.
INSERT_IDENTITY = {
    "category_rules": {"normalized_token"},
    "transaction_tags": {"transaction_id", "tag_id"},
}

TABLES = {t.name: t for t in Base.metadata.sorted_tables}


# ── snapshots ─────────────────────────────────────────────────────────────

async def _snap(conn) -> dict:
    out = {}
    for t in Base.metadata.sorted_tables:
        pk = [c.name for c in t.primary_key.columns]
        rows = (await conn.execute(select(t))).mappings().all()
        out[t.name] = {tuple(r[p] for p in pk): dict(r) for r in rows}
    return out


@dataclass
class Window:
    kind: str  # "preview" | "execute"
    tool: str
    pre: dict
    post: dict


def _kind(fn):
    for s in registry.all_tools():
        if fn is s.preview:
            return "preview", s
        if fn is s.execute:
            return "execute", s
    return None, None


@pytest.fixture
def windows(monkeypatch):
    out: list[Window] = []
    real = registry.call_mapped

    async def wrapper(fn, *a):
        kind, spec = _kind(fn)
        if kind is None:
            return await real(fn, *a)
        db = a[0].db
        await db.flush()
        pre = await _snap(await db.connection())
        res = await real(fn, *a)
        if kind == "preview":
            await db.flush()
            post = await _snap(await db.connection())
        else:  # execute commits: read the outcome from a fresh connection
            async with db.bind.connect() as conn:
                post = await _snap(conn)
        out.append(Window(kind, spec.name, pre, post))
        return res

    monkeypatch.setattr(registry, "call_mapped", wrapper)
    return out


# ── normalisation ─────────────────────────────────────────────────────────

def _norm(col, v):
    if v is None:
        return None
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, Decimal):
        scale = getattr(col.type, "scale", None)
        return f"{v:.{scale}f}" if scale is not None else str(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def _default(col):
    d = col.default
    if d is not None:
        assert d.is_scalar, f"cannot derive the default of {col.table.name}.{col.name}"
        return _norm(col, d.arg)
    if col.server_default is not None:
        raw = getattr(col.server_default.arg, "text", col.server_default.arg)
        assert isinstance(raw, str) and "(" not in raw, (
            f"cannot derive the server default of {col.table.name}.{col.name}"
        )
        raw = raw.strip("'")
        if col.type.python_type is bool:
            return raw in ("1", "true", "True")
        return col.type.python_type(raw)
    return None


def _exempt(table, col) -> bool:
    return table in BOOKKEEPING_TABLES or col in BOOKKEEPING_COLS or (table, col) in BOOKKEEPING


def _insert_exempt(table, col, natural_key: bool) -> bool:
    return (
        _exempt(table.name, col.name) or col.primary_key or col.name == "org_id"
        or (natural_key and col.name in INSERT_IDENTITY.get(table.name, ()))
    )


# ── the checks ────────────────────────────────────────────────────────────

def check_preview_window(w: Window) -> None:
    assert w.pre == w.post, f"rule 1: the {w.tool} preview wrote to the database"


def _locate(table, id_, pre, post, org_id):
    """('update'|'delete'|'insert'|'missing', pk or None)."""
    if isinstance(id_, str) and id_.startswith("new:"):
        return "insert", None
    if isinstance(id_, dict):
        assert table == "category_rules" and set(id_) == {"untrusted"}, f"rule 6: unresolvable id {id_!r}"
        tok = id_["untrusted"]

        def find(snap):
            return next((pk for pk, r in snap[table].items()
                         if r["org_id"] == org_id and r["normalized_token"] == tok), None)
        pk = find(pre) or find(post)
        if pk is None:
            return "missing", None
    else:
        pk = tuple(id_) if isinstance(id_, list) else (id_,)
    in_pre, in_post = pk in pre[table], pk in post[table]
    if in_pre and in_post:
        return "update", pk
    if in_pre:
        return "delete", pk
    return ("insert", pk) if in_post else ("missing", None)


def check_execute(w: Window, changes: list[dict], org_id: int, risk: str) -> None:
    pre, post = w.pre, w.post

    # 8. tier
    if risk == "write":
        assert not any(c["field"] == "balance_delta" for c in changes), (
            "rule 8: a write tool names a balance_delta"
        )
        moved = [pk for pk, r in post["accounts"].items()
                 if pk in pre["accounts"] and r["balance"] != pre["accounts"][pk]["balance"]]
        assert not moved, "rule 8: a write tool moved accounts.balance"

    groups: dict[tuple, list[dict]] = {}
    for c in changes:
        assert c["entity"] in TABLES, f"rule 3: unknown entity {c['entity']!r}"
        groups.setdefault((c["entity"], json.dumps(c["id"], sort_keys=True)), []).append(c)

    claimed: set[tuple] = set()
    ins_rows = {t: {pk for pk in post[t] if pk not in pre[t]} for t in TABLES if t not in BOOKKEEPING_TABLES}
    del_rows = {t: {pk for pk in pre[t] if pk not in post[t]} for t in TABLES if t not in BOOKKEEPING_TABLES}
    claimed_del: set[tuple] = set()
    insert_groups = []

    for (table, _), cs in groups.items():
        tobj = TABLES[table]
        id_ = cs[0]["id"]
        kind, pk = _locate(table, id_, pre, post, org_id)
        assert kind != "missing", f"rule 3: {table} {id_!r} names a row that does not exist"
        if kind == "update":
            for c in cs:
                f = c["field"]
                if f.endswith("_delta"):
                    base = f[: -len("_delta")]
                    assert (table, base) in DELTA_COLUMNS and c["before"] is None, (
                        f"rule 3: {table}.{f} is not a delta column"
                    )
                    d = Decimal(post[table][pk][base]) - Decimal(pre[table][pk][base])
                    assert d != 0 and d == Decimal(c["after"]), (
                        f"rule 3: {table}.{f} names {c['after']} but the column moved {d}"
                    )
                    claimed.add((table, pk, base))
                    continue
                assert f in tobj.c, f"rule 3: {table} has no column {f!r}"
                p, q = _norm(tobj.c[f], pre[table][pk][f]), _norm(tobj.c[f], post[table][pk][f])
                assert p == c["before"] and q == c["after"] and (p != q or c["before"] == c["after"]), (
                    f"rule 3: {table}.{f} names {c['before']!r} -> {c['after']!r}, row went {p!r} -> {q!r}"
                )
                if (table, f) not in DELTA_COLUMNS:  # a counter is only ever named as a delta
                    claimed.add((table, pk, f))
        elif kind == "delete":
            for c in cs:
                assert c["after"] is None, f"rule 5: {table} {id_!r} is deleted but names an after"
                assert c["field"] in tobj.c, f"rule 3: {table} has no column {c['field']!r}"
                assert _norm(tobj.c[c["field"]], pre[table][pk][c["field"]]) == c["before"], (
                    f"rule 5: {table}.{c['field']} before does not match the deleted row"
                )
            claimed_del.add((table, pk))
        else:
            insert_groups.append((table, id_, pk, cs))

    # 4. inserts: one-to-one, named values equal the row, the rest default
    for table, id_, pk, cs in insert_groups:
        tobj = TABLES[table]
        natural = not (isinstance(id_, str) and id_.startswith("new:"))
        named = {}
        for c in cs:
            assert c["before"] is None, f"rule 4: insert group {id_!r} names a before"
            named[c["field"]] = c["after"]
        assert set(named) <= set(tobj.c.keys()), f"rule 3: {table} has no column in {sorted(named)}"

        def problems(row):
            out = [f"{f}: row {_norm(tobj.c[f], row[f])!r}, preview {v!r}"
                   for f, v in named.items() if _norm(tobj.c[f], row[f]) != v]
            out += [f"{col.name}: unnamed, row {_norm(col, row[col.name])!r}, default {_default(col)!r}"
                    for col in tobj.c
                    if col.name not in named and not _insert_exempt(tobj, col, natural)
                    and _norm(col, row[col.name]) != _default(col)]
            return out

        cands = [pk] if pk is not None else sorted(ins_rows[table], key=str)
        assert cands, f"rule 4: insert group {table} {id_!r} matches no inserted row"
        for cand in cands:
            probs = problems(post[table][cand])
            if not probs:
                ins_rows[table].discard(cand)
                break
        else:
            raise AssertionError(f"rule 4: insert group {table} {id_!r} matches no row: {probs}")

    # 2. completeness: nothing changed that no Change names
    for table in ins_rows:
        extra = ins_rows[table]
        assert not extra, f"rule 2: undisclosed insert into {table}: {sorted(extra, key=str)}"
        leftover = {pk for pk in del_rows[table] if (table, pk) not in claimed_del}
        assert not leftover, f"rule 2: undisclosed delete from {table}: {sorted(leftover, key=str)}"
        for pk, row in post[table].items():
            if pk not in pre[table]:
                continue
            for col in TABLES[table].c:
                n = col.name
                if _exempt(table, n) or row[n] == pre[table][pk][n]:
                    continue
                assert (table, pk, n) in claimed, (
                    f"rule 2: {table} {pk} column {n} changed and no Change names it"
                )


def check_all(windows: list[Window], changes: list[dict], org_id: int, risk: str) -> None:
    previews = [x for x in windows if x.kind == "preview"]
    executes = [x for x in windows if x.kind == "execute"]
    assert len(previews) >= 2 and len(executes) == 1, "fence is vacuous: wrong number of windows"
    for p in previews:
        check_preview_window(p)
    check_execute(executes[0], changes, org_id, risk)


# ── running a scenario ────────────────────────────────────────────────────

async def honest_run(factory, windows, uid, tool, args, org_id, *, auto_token=None):
    kw = {"channel": "mcp", "scope": "agent:auto", "api_token_id": auto_token} if auto_token else {}
    out = await _invoke(factory, uid, tool, args, **kw)
    if out.get("status") != "done":
        out = await _confirm(factory, uid, out["action_id"])
    async with factory() as db:
        row = await db.get(AgentPendingAction, out["action_id"])
    assert row.status.value == "done"
    check_all(windows, row.preview_json["changes"], org_id, row.risk.value)
    return row.preview_json["changes"]


# ── T-A9 / T-A9b / T-A9c: the shipped write tools ─────────────────────────

TOKEN = normalize_description("SPOTIFY PREMIUM")


async def _add(f, *objs):
    async with f() as db:
        db.add_all(objs)
        await db.commit()


def _rule(a, category):
    return CategoryRule(
        org_id=a["org"], normalized_token=TOKEN, raw_description_seen="SPOTIFY PREMIUM",
        category_id=category, match_count=3, source=RuleSource("user_edit"),
    )


async def _none(f, a):
    return None


async def _rule_to_c1(f, a):
    await _add(f, _rule(a, a["c1"]))


async def _rule_to_c2(f, a):
    await _add(f, _rule(a, a["c2"]))


def _budget(a):
    return "budgets_update_amount", {"budget_id": a["b1"], "amount": "120.00"}


def _category(a):
    return "transactions_set_category", {"transaction_id": a["tx"], "category_id": a["c2"]}


# id, tool args builder, setup, auto
SCENARIOS = [
    ("budget-in-app", _budget, _none, False),
    ("budget-auto", _budget, _none, True),
    ("category-updates-an-existing-rule", _category, _rule_to_c1, False),
    ("category-creates-a-new-rule", _category, _none, False),
    ("category-onto-a-rule-already-there-is-a-no-op-change", _category, _rule_to_c2, False),
    ("category-auto-learns-no-rule", _category, _none, True),
]
COVERED = {build({"b1": 0, "tx": 0, "c2": 0})[0] for _, build, _, _ in SCENARIOS}


@pytest.mark.parametrize("sid,build,setup,auto", SCENARIOS, ids=[s[0] for s in SCENARIOS])
async def test_ta9_every_write_tool_tells_the_truth(factory, w, windows, sid, build, setup, auto):
    """FENCE T-A9 / GUARD T-A9c. Wrong implementation: any preview whose
    Changes differ from what execute writes (see the T-A10 mutants), or this
    fence going vacuous (window counts are asserted)."""
    a = w["A"]
    await setup(factory, a)
    tool, args = build(a)
    changes = await honest_run(
        factory, windows, a["member"], tool, args, a["org"], auto_token=a["t1"] if auto else None
    )
    if sid == "category-creates-a-new-rule":
        assert [c["entity"] for c in changes] == ["transactions", "category_rules"]
        assert changes[1]["before"] is None
    if sid == "category-onto-a-rule-already-there-is-a-no-op-change":
        assert changes[1]["before"] == changes[1]["after"] == a["c2"]
    if sid == "category-auto-learns-no-rule":
        assert [c["entity"] for c in changes] == ["transactions"]


def _uncovered():
    return sorted(s.name for s in registry.all_tools() if s.risk != "read" and s.name not in COVERED)


def test_ta9b_every_non_read_tool_has_a_scenario():
    """FENCE T-A9b. Wrong implementation: shipping a write tool the honesty
    fence never runs."""
    assert _uncovered() == []


# ── scratch mutants (T-A10, T-A10b) ───────────────────────────────────────

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_id: int


@pytest.fixture
def mutant():
    added: list[str] = []

    def _add_tool(name, changes, execute, *, risk="write", preview_hook=None):
        async def _preview(ctx, args):
            if preview_hook:
                await preview_hook(ctx)
            return Preview(summary="scratch", warnings=[], changes=changes(args), context={})

        async def _execute(ctx, args):
            await execute(ctx, args)
            await ctx.db.commit()
            return {"ok": True}

        registry.register(ToolSpec(
            name=name, risk=risk, args=_Args, product_area=None, min_role=Role.MEMBER,
            mirrors_route=("PUT", "/api/v1/budgets/{budget_id}"), description="t",
            preview=_preview, execute=_execute,
        ))
        added.append(name)
        return name

    yield _add_tool
    for n in added:
        registry._TOOLS.pop(n, None)


def _tx_row(a, **kw):
    return Transaction(**{
        "org_id": a["org"], "account_id": a["acct"], "category_id": a["c1"], "description": "NEW",
        "amount": Decimal("100.00"), "type": TransactionType.EXPENSE,
        "status": TransactionStatus.SETTLED, "date": P_START, "settled_date": P_START, **kw,
    })


def _tx_changes(a, amount="100.00", settled="__same__", status="settled"):
    cols = {
        "account_id": a["acct"], "category_id": a["c1"], "description": "NEW", "amount": amount,
        "type": "expense", "status": status, "date": P_START.isoformat(),
        "settled_date": P_START.isoformat() if settled == "__same__" else settled,
    }
    return [Change("transactions", "new:1", f, None, v) for f, v in cols.items()]


async def _b(ctx, bid, **values):
    await ctx.db.execute(update(Budget).where(Budget.id == bid).values(**values))


async def _run(factory, windows, w, name):
    a = w["A"]
    return await honest_run(factory, windows, a["member"], name, {"budget_id": a["b1"]}, a["org"])


def _b1(a, after="120.00"):
    return [Change("budgets", a["b1"], "amount", "100.00", after, "EUR")]


async def test_ta10_green_baseline_for_every_shape(factory, w, windows, mutant):
    """GUARD for the mutants below: the SAME harness is green for a correct
    update, insert and delete (else the reds prove nothing)."""
    a = w["A"]

    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"))
        ctx.db.add(_tx_row(a))
        await ctx.db.execute(Budget.__table__.delete().where(Budget.id == a["b2"]))

    name = mutant("m_ok", lambda args: _b1(a) + _tx_changes(a) + [
        Change("budgets", a["b2"], "amount", "50.00", None, "EUR"),
        Change("budgets", a["b2"], "category_id", a["c2"], None),
    ], ex)
    await _run(factory, windows, w, name)


MUTANTS = []


def _mutant(rule):
    def deco(fn):
        MUTANTS.append(pytest.param(fn, rule, id=fn.__name__))
        return fn
    return deco


@_mutant("rule 2")
def undisclosed_update(a):
    """Wrong implementation: execute also updates a second budget the preview never names."""
    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"))
        await _b(ctx, a["b2"], amount=Decimal("60.00"))
    return _b1(a), ex, {}


@_mutant("rule 3")
def wrong_after(a):
    """Wrong implementation: the named after is not what execute writes."""
    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("130.00"))
    return _b1(a), ex, {}


@_mutant("rule 2")
def undisclosed_column(a):
    """Wrong implementation: a named row also has a column changed that is not named."""
    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"), period_end=P_START)
    return _b1(a), ex, {}


@_mutant("rule 2")
def undisclosed_insert(a):
    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"))
        ctx.db.add(_tx_row(a))
    return _b1(a), ex, {}


@_mutant("rule 2")
def undisclosed_delete(a):
    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"))
        await ctx.db.execute(Budget.__table__.delete().where(Budget.id == a["b2"]))
    return _b1(a), ex, {}


@_mutant("rule 2")
def double_insert(a):
    """Wrong implementation: one named insert, two rows written."""
    async def ex(ctx, args):
        ctx.db.add_all([_tx_row(a), _tx_row(a)])
    return _tx_changes(a), ex, {}


@_mutant("rule 4")
def insert_value_1000_vs_100(a):
    async def ex(ctx, args):
        ctx.db.add(_tx_row(a, amount=Decimal("1000.00")))
    return _tx_changes(a), ex, {}


@_mutant("rule 4")
def named_column_left_null(a):
    """Wrong implementation: the create names settled_date, the row has NULL."""
    async def ex(ctx, args):
        ctx.db.add(_tx_row(a, status=TransactionStatus.PENDING, settled_date=None))
    return _tx_changes(a, settled="2026-11-01", status="pending"), ex, {}


@_mutant("rule 4")
def unnamed_column_not_default(a):
    """Wrong implementation: an insert sets a column the preview does not name."""
    async def ex(ctx, args):
        ctx.db.add(_tx_row(a, fitid="X"))
    return _tx_changes(a), ex, {}


@_mutant("rule 3")
def balance_delta_named_but_unmoved(a):
    """Wrong implementation: names a balance_delta for a row that contributes
    nothing (a REJECTED import row), so the balance never moves. Sensitive, so
    the tier rule cannot mask the delta rule."""
    async def ex(ctx, args):
        return None
    return [Change("accounts", a["acct"], "balance_delta", None, "9.99", "EUR")], ex, {"risk": "sensitive"}


@_mutant("rule 3")
def balance_delta_wrong_amount(a):
    async def ex(ctx, args):
        await ctx.db.execute(update(Account).where(Account.id == a["acct"]).values(balance=Decimal("1010.00")))
    return [Change("accounts", a["acct"], "balance_delta", None, "9.99", "EUR")], ex, {"risk": "sensitive"}


@_mutant("rule 2")
def balance_moved_named_absolute(a):
    """Wrong implementation: names the new absolute balance instead of a delta."""
    async def ex(ctx, args):
        await ctx.db.execute(update(Account).where(Account.id == a["acct"]).values(balance=Decimal("1010.00")))
    return [Change("accounts", a["acct"], "balance", "1000.00", "1010.00", "EUR")], ex, {"risk": "sensitive"}


@_mutant("rule 1")
def preview_adds_without_flush(a):
    """Wrong implementation: the preview ``db.add``s a row. Only the flush the
    fence performs makes it visible before propose's commit persists it."""
    async def hook(ctx):
        ctx.db.add(_tx_row(a, description="STRAY"))

    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"))
    return _b1(a), ex, {"preview_hook": hook}


@_mutant("rule 1")
def preview_writes_on_the_confirm_re_preview_only(a):
    calls = []

    async def hook(ctx):
        calls.append(1)
        if len(calls) > 1:
            ctx.db.add(_tx_row(a, description="STRAY"))

    async def ex(ctx, args):
        await _b(ctx, a["b1"], amount=Decimal("120.00"))
    return _b1(a), ex, {"preview_hook": hook}


@pytest.mark.parametrize("build,rule", MUTANTS)
async def test_ta10_the_fence_goes_red_for_each_mutant(factory, w, windows, mutant, build, rule):
    """FENCE T-A10. Each mutant is a tool that lies in one named way; the same
    harness that is green for the shipped tools must fail with the named rule."""
    a = w["A"]
    changes, ex, opts = build(a)
    name = mutant(f"m_{build.__name__}", lambda args: changes, ex, **opts)
    with pytest.raises(AssertionError, match=rule):
        await _run(factory, windows, w, name)


async def test_ta10b_a_write_tool_never_moves_or_names_money(factory, w, windows, mutant):
    """FENCE T-A10b. Wrong implementation: a write-risk (auto-eligible) tool
    that posts to accounts.balance, honestly disclosed."""
    a = w["A"]

    async def ex(ctx, args):
        await ctx.db.execute(update(Account).where(Account.id == a["acct"]).values(balance=Account.balance + 10))

    name = mutant("m_money", lambda args: [Change("accounts", a["acct"], "balance_delta", None, "10.00", "EUR")], ex)
    with pytest.raises(AssertionError, match="rule 8"):
        await _run(factory, windows, w, name)
    # the same tool is honest at sensitive tier
    windows.clear()
    name = mutant("m_money_sensitive", lambda args: [Change("accounts", a["acct"], "balance_delta", None, "10.00", "EUR")], ex, risk="sensitive")
    await _run(factory, windows, w, name)


def test_ta9b_the_detector_sees_an_unregistered_scenario(mutant):
    """FENCE T-A9b (detector). A write tool outside SCENARIOS is reported."""
    async def ex(ctx, args):
        return None
    name = mutant("m_uncovered", lambda args: [], ex)
    assert _uncovered() == [name]
