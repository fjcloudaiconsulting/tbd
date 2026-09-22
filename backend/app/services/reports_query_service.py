"""Reports v2 — AST → SQLAlchemy Core compiler + executor.

The architect-locked rule (spec §6 "Security model"):

- The AST is closed and validated by Pydantic before this module sees it.
- Compilation only consults whitelist tables on Transaction / Category /
  Account / Tag — never user-supplied strings.
- ``org_id`` is INJECTED here; the AST has no way to express it.
- All values reach the database as bound parameters (SQLAlchemy
  ``where()`` + ``bindparam`` semantics). No string interpolation.

Tag-filter semantics mirror the transactions list endpoint at
``backend/app/routers/transactions.py:90`` + the implementation at
``backend/app/services/transaction_service.py:1697``. ``tag_match=all``
(default) AND-combines every named tag; ``tag_match=any`` OR-combines.
"""
from __future__ import annotations

import time
from typing import Any, Tuple

from sqlalchemy import (
    Select,
    and_,
    distinct,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.reports_query import (
    MAX_LIMIT,
    resolve_truncated_end,
    Aggregation,
    Dimension,
    Filter,
    FilterField,
    FilterOp,
    Measure,
    MeasureField,
    ReportsQuery,
    TagMatch,
)
from app.services import currency_service
from app.services.transaction_filters import (
    effective_period_date_expr,
    non_reverted_transaction_filter,
    org_currency_filter,
    reciprocal_transfer_filter,
    reportable_transaction_filter,
    signed_amount_expr,
)


# Mapping: schema Dimension → (column expression factory, key string used
# in the response row). The factories take ``dialect_name`` so the time
# bucket dimensions can swap between MySQL ``DATE_FORMAT`` and SQLite
# ``strftime`` without leaking dialect logic into the router.


_DIM_KEYS: dict[Dimension, str] = {
    Dimension.CATEGORY: "category",
    Dimension.CATEGORY_MASTER: "category_master",
    Dimension.ACCOUNT: "account",
    Dimension.CURRENCY: "currency",
    Dimension.ACCOUNT_TYPE: "account_type",
    Dimension.TAG: "tag",
    Dimension.TXN_TYPE: "txn_type",
    Dimension.STATUS: "status",
    Dimension.MONTH: "month",
    Dimension.WEEK: "week",
    Dimension.DAY: "day",
}


def _currency_key():
    """The canonical currency expression: ``UPPER(TRIM(accounts.currency))``.

    ⚠ NOT the bare column, and not only for tidiness. ``accounts.currency`` was
    FREE TEXT before TBD-325 PR 1, and ``AccountUpdate`` carries no currency
    field (``schemas/account.py``), so a legacy ``'eur'`` row is unrepairable
    through the product. MySQL's ``utf8mb4_0900_ai_ci`` folds case inside GROUP
    BY and comparisons; SQLite does not -- and every CI shard except ``Migration
    Checks`` runs on aiosqlite. A bare column therefore splits ``'eur'`` and
    ``'EUR'`` into two partitions, and drops the lowercase row from a filter,
    ON CI ONLY. The same normalise-BOTH-sides rule
    ``currency_service.resolve_currency_scope`` spells out.

    One function, used by the dimension AND the filter, so the group key and the
    predicate cannot drift apart into the case where a filter matches rows the
    grouping then splits.
    """
    return func.upper(func.trim(Account.currency))


def _dimension_expr(dim: Dimension, dialect_name: str):
    """Return the SQLAlchemy expression for a dimension column.

    Time-bucket dimensions use MySQL ``DATE_FORMAT`` in production and
    SQLite ``strftime`` under pytest. This keeps the compiler dialect-
    agnostic from the AST perspective.
    """
    if dim is Dimension.CATEGORY:
        return Category.name
    if dim is Dimension.CATEGORY_MASTER:
        # Parent category name when the row's category has a parent;
        # else the category's own name. Computed via COALESCE on
        # parent.name + own name. Joining the parent table happens in
        # the JOIN block below.
        return func.coalesce(
            _category_parent_name_label(), Category.name
        )
    if dim is Dimension.ACCOUNT:
        return Account.name
    if dim is Dimension.CURRENCY:
        return _currency_key()
    if dim is Dimension.ACCOUNT_TYPE:
        # ⚠ ``AccountType.name``, never ``slug``. ``routers/account_types.py``
        # never assigns a slug, so every user-created type has ``slug IS NULL``
        # and a slug-keyed report silently returns 0.00 for them. Same choice
        # ``reports/sources/accounts.py`` already made; do not diverge.
        # ⚠ Two same-named types collapse into one bar. That is a pre-existing
        # property of the accounts source, inherited here for consistency.
        return AccountType.name
    if dim is Dimension.TAG:
        return Tag.name
    if dim is Dimension.TXN_TYPE:
        return Transaction.type
    if dim is Dimension.STATUS:
        return Transaction.status
    # Time-bucket dimensions. Cash-basis: bucket by the effective settled
    # date (coalesce(settled_date, date)) so a row dated in month X but
    # settled in month Y is counted in Y, consistent with the list/forecast.
    eff = effective_period_date_expr()
    if dim is Dimension.MONTH:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%m", eff)
        return func.date_format(eff, "%Y-%m")
    if dim is Dimension.WEEK:
        if dialect_name == "sqlite":
            # Year + ISO week. SQLite's strftime("%W") is "week of year
            # zero-padded (00..53)" — close enough for the AST contract,
            # mirrors MySQL's WEEK() default mode.
            return func.strftime("%Y-%W", eff)
        return func.date_format(eff, "%x-%v")
    if dim is Dimension.DAY:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%m-%d", eff)
        return func.date_format(eff, "%Y-%m-%d")
    raise ValueError(f"unsupported dimension {dim!r}")


_PARENT_CATEGORY_ALIAS = None


def _category_parent_name_label():
    """Return a column expression that evaluates to the parent category
    name. We lazily create an alias on Category so the JOIN block can
    reference it. The alias is reused across the compiler.
    """
    global _PARENT_CATEGORY_ALIAS
    if _PARENT_CATEGORY_ALIAS is None:
        from sqlalchemy.orm import aliased

        _PARENT_CATEGORY_ALIAS = aliased(Category, name="category_master")
    return _PARENT_CATEGORY_ALIAS.name


def _category_parent_alias():
    """Return the aliased Category used as the parent-side join.
    Created lazily; the dimension expression and the JOIN block both
    consult this so they stay in sync.
    """
    global _PARENT_CATEGORY_ALIAS
    if _PARENT_CATEGORY_ALIAS is None:
        from sqlalchemy.orm import aliased

        _PARENT_CATEGORY_ALIAS = aliased(Category, name="category_master")
    return _PARENT_CATEGORY_ALIAS


_FILTER_COLUMN: dict[FilterField, Any] = {
    # Cash-basis: the DATE filter compares against the effective settled
    # date (coalesce(settled_date, date)), so a date-window filter buckets
    # rows by when they settled — consistent with the time dimensions.
    FilterField.DATE: effective_period_date_expr(),
    FilterField.AMOUNT: Transaction.amount,
    FilterField.CATEGORY_ID: Transaction.category_id,
    FilterField.ACCOUNT_ID: Transaction.account_id,
    FilterField.TXN_TYPE: Transaction.type,
    FilterField.STATUS: Transaction.status,
    # TAG_NAME is handled separately because it needs a join +
    # subquery (mirrors the transactions list semantics).
}


def _coerce_enum_value(field: FilterField, value):
    """Coerce a wire string into the SQLAlchemy enum value where
    necessary so the comparison binds correctly. Decimal / int / date
    pass through unchanged.
    """
    if field is FilterField.TXN_TYPE:
        return TransactionType(value)
    if field is FilterField.STATUS:
        return TransactionStatus(value)
    return value


def _apply_scalar_filter(stmt: Select, f: Filter) -> Select:
    col = _FILTER_COLUMN[f.field]
    op = f.op
    if op is FilterOp.EQ:
        return stmt.where(col == _coerce_enum_value(f.field, f.value))
    if op is FilterOp.GTE:
        return stmt.where(col >= _coerce_enum_value(f.field, f.value))
    if op is FilterOp.LTE:
        return stmt.where(col <= _coerce_enum_value(f.field, f.value))
    if op is FilterOp.IN:
        values = [_coerce_enum_value(f.field, v) for v in f.value]
        return stmt.where(col.in_(values))
    if op is FilterOp.BETWEEN:
        lo, hi = f.value
        return stmt.where(col.between(lo, hi))
    raise ValueError(f"unsupported op {op!r}")


# TBD-507. ⚠ The sentence for a figure that ADDS currencies together. It is a
# different fact from ``currency_service.currency_warning``'s, which says money
# was left OUT, so it is a different sentence -- and it is inlined here rather
# than put beside that one because it has exactly one consumer. The remedy is
# named, because the user can act on it: the currency dimension now exists.
# ⚠ Opening clause matches ``currency_warning`` and ``networth.py``'s notice
# verbatim -- three sentences in one family, and the user may see two of them on
# one canvas.
_MIXED_CURRENCY_WARNING = (
    "Multiple currencies held; this figure adds them together, because the "
    "filter selected several and the rows are not grouped by currency. Add "
    "Currency as a dimension to separate them."
)


def _selected_currencies(ast: ReportsQuery) -> set[str] | None:
    """The currency codes this AST's filters admit; ``None`` means unrestricted.

    Several currency filters AND together, so the admitted set is their
    intersection -- ``currency eq EUR`` plus ``currency eq USD`` admits nothing,
    which is an empty set, not "unrestricted".
    """
    selected: set[str] | None = None
    for f in ast.filters:
        if f.field is not FilterField.CURRENCY:
            continue
        codes = set(f.value) if f.op is FilterOp.IN else {f.value}
        selected = codes if selected is None else selected & codes
    return selected


# TBD-507. How a query treats currency. THREE states, ONE function.
#
# ⚠ Deliberately not two booleans. The first cut had one predicate answering two
# questions and shipped a silent cross-currency sum; the obvious repair is a
# second boolean, but two booleans describe FOUR states and only three are
# reachable, so the fourth ("scoped, yet mixing") is a bug waiting for someone
# to construct it. One function with three answers cannot disagree with itself,
# which is the same argument ``org_currency_filter`` makes for taking the whole
# scope dict instead of a currency string.
#
# Compare against these NAMES, never against the bare strings: a mistyped name
# is a NameError, a mistyped literal is a silently false branch.
_SCOPED = "scoped"          # caller said nothing -> scope to the primary currency
_PARTITIONED = "partitioned"  # every row is exactly one currency
_MIXED = "mixed"            # rows can span currencies, and nobody asked them to


def _currency_mode(ast: ReportsQuery) -> str:
    """Return ``_SCOPED`` / ``_PARTITIONED`` / ``_MIXED`` for this AST.

    * No mention of currency -> ``_SCOPED``. Unchanged TBD-325 behaviour: a user
      who did not ask to see the split must not be handed a cross-currency sum.
    * ``Dimension.CURRENCY`` requested -> ``_PARTITIONED``, whatever the filters
      say, because the group key separates every row by currency.
    * A filter naming exactly ONE code -> ``_PARTITIONED``. Every surviving row
      is that currency.
    * A filter naming several -> ``_MIXED``. The rows are summed across
      currencies with no column to tell them apart, so the notice has to say so.
    * A filter naming NONE -- two ``eq`` filters that contradict, so the
      intersection is empty -> also ``_MIXED``. The result is zero rows and the
      notice describes a figure that does not exist, which is over-warning on
      the safe side: never ``_SCOPED``, never a silent number. Deliberate.

    ⚠ ``_SCOPED`` must not be returned when a currency filter is present, even
    though that looks conservative: the scope says EUR and the filter says USD,
    the conjunction is unsatisfiable, and the user gets an empty chart with no
    explanation.

    ⚠ Derived from the AST ALONE, never from ``currency_scope``. A scope-derived
    version would read ``excluded_account_count``, which is 0 for a
    NULL-``primary_currency`` legacy multi-currency org (the exclusion
    comprehension in ``currency_service.resolve_currency_scope`` requires a
    non-NULL primary) -- precisely the cohort most able to mix, so it would go
    silent exactly where the sentence matters. See TBD-551. The price is
    over-warning: an all-EUR org asking for ``currency in ["EUR","USD"]`` is
    told the figure can mix when it cannot. Cheap, and it cannot go silent.
    """
    if Dimension.CURRENCY in ast.dimensions:
        return _PARTITIONED
    selected = _selected_currencies(ast)
    if selected is None:
        return _SCOPED
    return _PARTITIONED if len(selected) == 1 else _MIXED


def _asks_for_transfers(ast: ReportsQuery) -> bool:
    """Did the caller explicitly ask to SEE transfer legs? (TBD-471)"""
    return any(
        f.field is FilterField.TRANSFER and f.value is True for f in ast.filters
    )


def _reportability_base(ast: ReportsQuery):
    """The ONE base reportability clause for this query.

    By default Reports exclude transfer legs, manual balance adjustments and
    reverted reconciliation rows, so a transactions report matches Budgets /
    Forecast / Sankey. ``include_non_reportable`` is the shipped opt-in that
    re-includes the first two.

    ⚠⚠ ``transfer=true`` MUST ALSO PROMOTE THE BASE, and this is the whole
    reason this is a named function rather than an ``if`` at the call site.
    ``reportable_transaction_filter()``'s first term is
    ``linked_transaction_id IS NULL``; conjoined with a filter that demands a
    reciprocal link it is UNSATISFIABLE -- zero rows, no error, no warning.
    That is the same silent-empty defect the dead ``Type = Transfer`` checkbox
    already ships, and reproducing it while fixing it would be absurd.

    ⚠ The precedent is ``_currency_mode`` above, one ticket old: an EXPLICIT
    request stands a DEFENSIVE DEFAULT down, decided in one place, from the AST
    alone. The alternative shapes were both rejected in review -- an inline
    ``or`` bolted onto the call site (an invisible cross-field coupling nobody
    reading a saved widget's JSON could find) and a 422 refusing the pair
    (which refuses the one query this ticket exists to enable; the default
    exists only to say "unless asked", and ticking "only transfers" IS asking).

    ⚠ It reads BOTH inputs. A refactor that keys the base on the transfer
    filter alone breaks the shipped ``include_non_reportable`` toggle, which is
    an independent request; G2 fences that direction.
    """
    if ast.include_non_reportable or _asks_for_transfers(ast):
        return non_reverted_transaction_filter()
    return reportable_transaction_filter()


def _apply_currency_filter(stmt: Select, f: Filter, org_id: int) -> Select:
    """Filter to transactions on accounts denominated in the given currency.

    ⚠ A CORRELATED SUBQUERY, not a ``_FILTER_COLUMN`` entry plus a join. The
    generic scalar path would need ``Account`` joined for every filtered query,
    changing row multiplicity on statements that already join -- the same reason
    ``transaction_filters.org_currency_filter`` is shaped this way. Values are
    already normalised by the AST validator
    (``schemas/reports_query.py`` uppercases and length-checks the code) and the
    stored column is normalised here, so BOTH sides are canonical.
    """
    # ⚠ RAISE, never fall through to ``eq``. The catalog publishes only
    # ``("eq", "in")`` so nothing else reaches here today, but the AST layer
    # would accept ``gte``/``lte`` on this field, and an unsupported op silently
    # treated as ``eq`` returns a confidently wrong answer the day someone
    # widens the ops tuple. ``_apply_tag_filter`` and ``accounts.py`` both raise.
    if f.op is FilterOp.IN:
        values = list(f.value)
    elif f.op is FilterOp.EQ:
        values = [f.value]
    else:
        raise ValueError(f"currency: unsupported op {f.op.value!r}")
    # ⚠ NOT re-normalised here. ``schemas/reports_query._coerce_filter_scalar``
    # already strips, uppercases and rejects anything that is not three alpha
    # characters, and it runs on every ``Filter`` construction. A second
    # normalisation is a second thing to drift.
    return stmt.where(
        Transaction.account_id.in_(
            select(Account.id).where(
                Account.org_id == org_id,
                _currency_key().in_(values),
            )
        )
    )


def _apply_tag_filter(stmt: Select, f: Filter, org_id: int) -> Select:
    """Tag filter — mirrors transactions list semantics.

    ``tag_match=all``: each named tag yields its own IN subquery,
    AND-combined via repeated ``stmt.where(...)``.
    ``tag_match=any``: a single IN subquery against the union of
    named tags, OR-combined.

    Reference: ``backend/app/services/transaction_service.py:1697``.
    """
    # Coerce to a list of normalized names.
    if f.op is FilterOp.IN:
        names = [str(v).strip().lower() for v in f.value if str(v).strip()]
    elif f.op is FilterOp.EQ:
        names = [str(f.value).strip().lower()]
    else:
        raise ValueError(
            f"tag_name filter only supports op='eq' / op='in'; got {f.op!r}"
        )
    if not names:
        return stmt

    if f.tag_match is TagMatch.ANY:
        return stmt.where(
            Transaction.id.in_(
                select(TransactionTag.transaction_id)
                .join(Tag, Tag.id == TransactionTag.tag_id)
                .where(
                    Tag.org_id == org_id,
                    Tag.name_normalized.in_(names),
                )
            )
        )
    # ALL semantics: one IN-subquery per name, AND-combined.
    for name in names:
        stmt = stmt.where(
            Transaction.id.in_(
                select(TransactionTag.transaction_id)
                .join(Tag, Tag.id == TransactionTag.tag_id)
                .where(
                    Tag.org_id == org_id,
                    Tag.name_normalized == name,
                )
            )
        )
    return stmt


def _measure_expr(measure: Measure):
    """Translate a Measure into a SQLAlchemy aggregate expression."""
    agg = measure.agg
    field = measure.field
    col_map = {
        MeasureField.AMOUNT: Transaction.amount,
        MeasureField.ID: Transaction.id,
        MeasureField.CATEGORY_ID: Transaction.category_id,
        MeasureField.ACCOUNT_ID: Transaction.account_id,
        # TBD-553. A col_map ROW, not a branch before the lookup: the entry
        # exists, so there is no KeyError/500 path to order around.
        MeasureField.NET_AMOUNT: signed_amount_expr(),
    }
    col = col_map[field]
    if agg is Aggregation.SUM:
        return func.coalesce(func.sum(col), 0).label("value")
    if agg is Aggregation.COUNT:
        return func.count(col).label("value")
    if agg is Aggregation.AVG:
        return func.coalesce(func.avg(col), 0).label("value")
    if agg is Aggregation.DISTINCT:
        return func.count(distinct(col)).label("value")
    raise ValueError(f"unsupported aggregation {agg!r}")


def compile_ast_to_query(
    ast: ReportsQuery,
    *,
    org_id: int,
    dialect_name: str = "mysql",
    overfetch: bool = False,
    currency_scope: dict | None = None,
) -> Select:
    """Compile a validated ``ReportsQuery`` AST into a SQLAlchemy Core
    ``Select`` bound to a single org.

    The AST has no way to specify ``org_id``; it is injected here from
    the authenticated caller's context. All other values reach the
    statement as bound parameters (SQLAlchemy's default).

    ``overfetch=True`` asks for ONE row beyond the effective limit — the
    probe row that lets ``execute_query`` tell "a complete result that
    exactly fills the limit" apart from "there was more". The caller MUST
    slice it off before it reaches the payload; ``execute_query`` does.
    """
    # 1) Measure projection + base select.
    measure_expr = _measure_expr(ast.measure)
    dim_exprs: list[tuple[str, Any]] = []
    for dim in ast.dimensions:
        key = _DIM_KEYS[dim]
        expr = _dimension_expr(dim, dialect_name).label(key)
        dim_exprs.append((key, expr))

    stmt = select(*[expr for _, expr in dim_exprs], measure_expr).select_from(
        Transaction
    )

    # 2) Joins driven by dimension requirements.
    requested = {dim for dim in ast.dimensions}
    if Dimension.CATEGORY in requested or Dimension.CATEGORY_MASTER in requested:
        stmt = stmt.join(Category, Category.id == Transaction.category_id)
    if Dimension.CATEGORY_MASTER in requested:
        parent = _category_parent_alias()
        stmt = stmt.outerjoin(parent, parent.id == Category.parent_id)
    # ⚠ CURRENCY rides the SAME join, and the join is REQUIRED, not an
    # optimisation. Referencing ``Account.currency`` under
    # ``select_from(Transaction)`` with no join renders ``FROM transactions,
    # accounts`` -- a CROSS JOIN that does not raise, it silently multiplies
    # every row by the org's account count. ``or`` rather than a second
    # ``.join()`` because joining the same table twice raises at compile time
    # for ACCOUNT + CURRENCY together, which is an ordinary query.
    if (
        Dimension.ACCOUNT in requested
        or Dimension.CURRENCY in requested
        or Dimension.ACCOUNT_TYPE in requested
    ):
        stmt = stmt.join(Account, Account.id == Transaction.account_id)
    # ⚠ CHAINED off the Account join, never added as a second independent one:
    # joining ``accounts`` twice raises at compile time, and ACCOUNT +
    # ACCOUNT_TYPE together is an ordinary pick (MAX_DIMENSIONS is 2).
    # INNER is safe -- ``Account.account_type_id`` is NOT NULL.
    if Dimension.ACCOUNT_TYPE in requested:
        stmt = stmt.join(AccountType, AccountType.id == Account.account_type_id)
    if Dimension.TAG in requested:
        stmt = stmt.join(
            TransactionTag, TransactionTag.transaction_id == Transaction.id
        ).join(Tag, Tag.id == TransactionTag.tag_id)

    # 3) Filters. ``org_id`` is the load-bearing WHERE; appended unconditionally.
    stmt = stmt.where(Transaction.org_id == org_id)

    # Reportability. By default Reports exclude transfer legs, manual balance
    # adjustments, and reverted (skipped/rejected) reconciliation rows, so a
    # transaction-source report matches Budgets / Forecast / Sankey. The opt-in
    # ``include_non_reportable`` flag re-includes transfer legs + manual
    # adjustments; reverted rows stay excluded either way (their amount was
    # reverted from the account balance, so counting them double-counts). On
    # the opt-in path that means skipped/rejected rows AND reconcile-matched
    # duplicates, which ``non_reverted_transaction_filter()`` drops via
    # ``balance_contribution_filter()``'s one-way-link arm (TBD-470).
    # This compiler only ever builds on ``Transaction`` (the transactions
    # source), so the clause is transactions-scoped by construction.
    # ⚠ TBD-471: ONE function decides this, and it reads the transfer filter as
    # well as ``include_non_reportable``. See ``_reportability_base``.
    stmt = stmt.where(_reportability_base(ast))

    # TBD-325 PR 2, as amended by TBD-507. Currency lives only on
    # ``accounts.currency``, so an unscoped ``SUM(amount)`` here adds EUR to USD
    # unless the rows are separated by something that implies a currency.
    #
    # ⚠ HISTORY, because the reason this clause exists is no longer the reason
    # it was written: until TBD-507 the transactions catalog was the only one of
    # the FIVE sources publishing no currency dimension and no currency filter
    # (``accounts.py``, ``recurring.py``, ``networth.py`` and
    # ``credit_utilization.py`` all did), so the user had no way to separate the
    # money by hand and scoping was the only defence. Transactions now publishes
    # both, which is why the clause below is conditional rather than absolute.
    #
    # ⚠ The clause goes on the WHERE, never inside ``_measure_expr``: currency
    # enters at the GROUP BY / filter level, not at the aggregate. Scoping the
    # measure would also silently apply to COUNT and AVG, which is a different
    # question.
    #
    # TBD-507. The scope STANDS DOWN when the caller explicitly asked about
    # currency, and is otherwise unchanged.
    #
    # ⚠ Without the stand-down the feature ships broken in BOTH directions, not
    # merely conservative: ``dimensions=[CURRENCY]`` returns exactly ONE row (a
    # currency breakdown with a single bar, which looks authoritative), and
    # ``filters=[currency in ["USD"]]`` returns ZERO rows, because the scope
    # says EUR and the filter says USD and the conjunction is unsatisfiable.
    #
    # ⚠ Partition ON REQUEST, never unconditionally. The always-group-then-pop
    # pattern in ``reports/sources/credit_utilization.py`` and ``networth.py``
    # is NOT portable here: those two sort and slice in PYTHON over the full
    # grouped set, so their "is the whole result one currency?" predicate sees
    # every row, while this compiler applies a SQL ``LIMIT`` at step 6 -- the
    # same predicate would only ever see a PAGE, and a two-currency org whose
    # first page sorted all-EUR would have the key popped and be reported as
    # single-currency. Unconditional grouping also breaks the KPI shape
    # (``dimensions=[]``, ``limit=1``), which reads row zero as THE total.
    if _currency_mode(ast) == _SCOPED:
        stmt = stmt.where(org_currency_filter(org_id, currency_scope))

    for f in ast.filters:
        if f.field is FilterField.TAG_NAME:
            stmt = _apply_tag_filter(stmt, f, org_id)
        elif f.field is FilterField.CURRENCY:
            stmt = _apply_currency_filter(stmt, f, org_id)
        elif f.field is FilterField.TRANSFER:
            # ⚠ RAISE, never fall through to ``eq`` -- the same rule
            # ``_apply_currency_filter`` states above, and it bites HARDER here.
            # The catalog publishes ``("eq",)`` so nothing else reaches this
            # today, but if the tuple is ever widened to ``("eq","in")`` then
            # ``f.value`` becomes a LIST: truthy, so the clause applies, while
            # ``_asks_for_transfers`` tests ``is True`` and is False for a list,
            # so the base does NOT stand down -- giving
            # ``link IS NULL AND EXISTS(reciprocal)``, unsatisfiable, zero rows,
            # no error. That is this ticket's own defect class, reintroduced.
            if f.op is not FilterOp.EQ:
                raise ValueError(f"transfer: unsupported op {f.op.value!r}")
            # ``_coerce_filter_scalar`` has already reduced the value to a bool.
            clause = reciprocal_transfer_filter()
            stmt = stmt.where(clause if f.value else ~clause)
        else:
            stmt = _apply_scalar_filter(stmt, f)

    # 4) GROUP BY (only when there are dimensions).
    if dim_exprs:
        # Group by the raw (unlabeled) expressions; aliased labels can
        # confuse some MySQL versions inside GROUP BY.
        raw_group_cols = []
        for dim in ast.dimensions:
            raw_group_cols.append(_dimension_expr(dim, dialect_name))
        stmt = stmt.group_by(*raw_group_cols)

    # 5) ORDER BY.
    sort = ast.sort
    if sort is not None:
        if sort.by.value == "value":
            order_col = literal_column("value")
        else:
            # Sort by the first dimension column when ``dimension`` is
            # requested (no AST way to pick a specific dim yet — spec
            # §6 keeps the surface narrow).
            if not dim_exprs:
                raise ValueError(
                    "sort.by='dimension' requires at least one dimension"
                )
            order_col = literal_column(dim_exprs[0][0])
        if sort.dir.value == "asc":
            stmt = stmt.order_by(order_col.asc())
        else:
            stmt = stmt.order_by(order_col.desc())
    elif dim_exprs:
        # Stable default order by value desc.
        stmt = stmt.order_by(literal_column("value").desc())

    # 6) Hard limit cap. Pydantic already enforced ``limit <= 500`` on
    # the AST; the ``min()`` is defence-in-depth in case the AST is ever
    # constructed in Python without going through validation.
    #
    # ⚠ The ``+ 1`` probe rides on the CAPPED value, never on ``ast.limit``:
    # a request at exactly ``MAX_LIMIT`` must still be able to report
    # ``truncated: true``, and capping after the ``+1`` would swallow the
    # probe row and make truncation unreportable at the cap.
    stmt = stmt.limit(min(ast.limit, MAX_LIMIT) + (1 if overfetch else 0))
    return stmt


# ─── execution helpers ──────────────────────────────────────────────


# Spec §6 "Hard caps" — "Per-request query timeout 5 s". MySQL 8.0
# supports the ``MAX_EXECUTION_TIME(ms)`` optimizer hint inside the
# statement, so we attach it as a SELECT prefix at compile time. The
# hint binds the limit to THIS statement only (not the connection or
# the pool), so a long-running report can't poison the next request.
#
# Under SQLite (pytest) the hint is meaningless: SQLite ignores
# unrecognized comment-form hints, but ``prefix_with`` also injects it
# OUTSIDE the comment markers. To keep the test backend happy we only
# attach the hint when the dialect is MySQL.
QUERY_TIMEOUT_MS = 5000
_MYSQL_TIMEOUT_HINT = f"/*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */"


def _apply_query_timeout(stmt: Select, dialect_name: str) -> Select:
    """Attach a per-statement timeout to the compiled SELECT.

    MySQL: inject the ``MAX_EXECUTION_TIME`` optimizer hint via
    ``prefix_with`` so it lands right after the ``SELECT`` keyword. The
    server aborts the query (and only the query — not the connection)
    once the wall-clock exceeds ``QUERY_TIMEOUT_MS``.

    SQLite (pytest harness): no-op. SQLite does not understand the hint
    syntax and tests don't need the cap. The router still relies on the
    Pydantic-validated AST + ``MAX_LIMIT`` to keep test queries small.
    """
    if dialect_name == "mysql":
        return stmt.prefix_with(_MYSQL_TIMEOUT_HINT, dialect="mysql")
    return stmt


def _currency_notice(currency_mode: str, currency_scope: dict | None) -> str | None:
    """The ``meta.warning`` for a currency mode. THREE outcomes, not two.

    * ``_SCOPED`` -> the TBD-325 exclusion sentence (``None`` when nothing was
      excluded, which is every org in production today).
    * ``_PARTITIONED`` -> ``None``. Nothing was excluded and every row names its
      own currency, so any sentence here would be a lie.
    * ``_MIXED`` -> say the figure adds currencies together. ⚠ This is the case
      an earlier cut got wrong, returning an unlabelled EUR+USD total with
      ``warning: null``.
    """
    if currency_mode == _SCOPED:
        return currency_service.currency_warning(currency_scope)
    if currency_mode == _MIXED:
        return _MIXED_CURRENCY_WARNING
    return None


async def execute_query(
    db: AsyncSession,
    ast: ReportsQuery,
    *,
    org_id: int,
) -> Tuple[list[dict], dict]:
    """Execute a validated AST and return ``(rows, meta)``.

    Returns dicts keyed by dimension name plus ``"value"``. Caller
    serializes to ``ReportsQueryResponse``.

    Per-request query timeout (spec §6 "Hard caps"): on MySQL the
    compiled SELECT carries a ``MAX_EXECUTION_TIME(5000)`` optimizer
    hint so the server aborts a runaway aggregation after 5 s without
    poisoning the connection. SQLite (the pytest backend) does not
    understand the hint and silently skips it; the small fixture
    datasets used in tests finish in milliseconds, so the gap is
    intentional rather than a defect.
    """
    try:
        dialect = db.get_bind().dialect.name
    except Exception:
        dialect = "mysql"
    # ⚠ Skipped entirely for an opted-in query: both consumers below are gated
    # on the currency mode, so resolving it would be one round trip per
    # report whose result is discarded. ``org_currency_filter(org_id, None)`` returns
    # ``true()``, so the ``None`` is safe even on the path that still compiles
    # the clause. ``org_currency_filter``'s own docstring counts statements for
    # exactly this reason.
    currency_mode = _currency_mode(ast)
    currency_scope = (
        await currency_service.resolve_currency_scope(db, org_id=org_id)
        if currency_mode == _SCOPED
        else None
    )
    stmt = compile_ast_to_query(
        ast,
        org_id=org_id,
        dialect_name=dialect,
        overfetch=True,
        currency_scope=currency_scope,
    )
    stmt = _apply_query_timeout(stmt, dialect)
    started = time.perf_counter()
    result = await db.execute(stmt)
    rows = result.mappings().all()
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # ⚠ ``truncated`` means "there was MORE than we returned" (TBD-484).
    # It CANNOT be derived from ``out_rows`` after a SQL ``LIMIT n``:
    # ``len(out_rows) >= n`` is true for every complete result that
    # happens to fill the limit exactly, which is an ordinary shape (the
    # seeded widget limits are 10, 50 and 100). So we fetch ``n + 1``,
    # read the answer off the probe row, then slice it away — ``row_count``
    # and the payload stay exactly what the client asked for.
    cap = min(ast.limit, MAX_LIMIT)
    truncated = len(rows) > cap
    rows = rows[:cap]

    out_rows = []
    for r in rows:
        d = {}
        for key, _ in [
            (_DIM_KEYS[dim], None) for dim in ast.dimensions
        ]:
            d[key] = r.get(key)
        # Coerce SUM(amount) Decimal → float for JSON.
        val = r.get("value")
        if hasattr(val, "as_tuple"):  # Decimal-like
            try:
                d["value"] = float(val)
            except Exception:  # pragma: no cover - defensive
                d["value"] = str(val)
        else:
            d["value"] = val
        out_rows.append(d)

    meta = {
        "row_count": len(out_rows),
        "truncated": truncated,
        # ⚠ Derived from the ORDER BY this query ACTUALLY ran with, not from
        # the dataset. Step 5 above orders by ``sort`` (defaulting to value
        # DESC), so a caller passing ``dir: "asc"`` over ``month`` — the shape
        # every seeded line/area/stacked_bar widget sends — truthfully reports
        # "newest" instead of a source-keyed map's "lowest-ranked".
        "truncated_end": (
            resolve_truncated_end(ast.sort, ast.dimensions) if truncated else None
        ),
        "query_ms": elapsed_ms,
        # TBD-325 PR 2. ⚠ SCOPE AND SAY SO. The clause above silently drops a
        # multi-currency org's non-primary rows, and silent is the one thing
        # this ticket exists to stop: the numbers become right while the
        # explanation goes missing. Sankey carries the same declaration through
        # the same ``QueryMeta.warning`` field, and a reports widget can sit
        # beside a Sankey widget on one dashboard canvas, so the two must not
        # differ in whether they admit what was excluded.
        #
        # ⚠ TBD-507 NARROWED THAT PARITY TO THE ``_SCOPED`` PATH, and left
        # sankey alone. ``build_sankey`` emits a GRAPH, not rows, so there is no
        # group-key column to pop: partitioning it means per-currency node
        # identities, which breaks ``frontend/lib/reports/sankey-labels.ts``'s
        # literal-keyed map, the nivo node ids and the CSV export -- and nivo
        # cannot draw two disconnected graphs in one canvas, so the real answer
        # is probably two widgets, which is a product call. Tracked as TBD-549.
        # Until then: a currency-mentioning reports query partitions while the
        # sankey beside it still scopes. They differ deliberately, and each says
        # what it did.
        #
        # Three outcomes, not two; see ``_currency_notice`` above for which and
        # why.
        "warning": _currency_notice(currency_mode, currency_scope),
    }
    return out_rows, meta
