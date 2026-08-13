"""TBD-345: ``./pfv seed`` must produce the dataset it claims to produce.

CLAUDE.md documented ``./pfv seed`` as "a repeatable local dataset". It was
not: ``random`` was imported and never seeded, and every date derived from
``date.today()``, so the geometry changed with the day of the month.

``seed.py`` drives the real HTTP API against a live backend, so none of this
could be tested until the geometry was extracted into pure planners. These
fences run against those planners: no server, no database, no ``httpx``.

Each test below names the specific wrong implementation it kills. That is
deliberate — this repo has shipped a fence that was green against unmodified
``main`` roughly seventeen times, and "the tests pass" is not evidence that
they could ever have failed.

Companion to ``test_seed_billing_period_contract.py``, which guards the
request SHAPE at the AST level; this module guards the dataset's CONTENT.
"""
from __future__ import annotations

import ast
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

import seed
from app.models.category import CategoryType
from app.services import org_bootstrap_service


SEED_PY = Path(__file__).resolve().parents[1] / "seed.py"

# Far from the wall clock in both directions, so a planner that secretly reads
# `date.today()` cannot coincidentally agree with one that honours the anchor.
FAR_ANCHOR = date(2027, 3, 17)


class _ExplodingDate(date):
    """A ``date`` whose ``today()`` raises.

    Everything else (``fromisoformat``, arithmetic, ``replace``) still works,
    so patching this in isolates exactly one question: did anything in the
    planners read the wall clock?
    """

    @classmethod
    def today(cls):  # pragma: no cover - the point is that it never runs
        raise AssertionError(
            "a planner read date.today(); the anchor must be the only clock"
        )


# ── The RNG must actually be the injected one ────────────────────────────


def test_plan_transactions_uses_the_injected_rng():
    """Same anchor + same seed ⇒ identical plan. Different seed ⇒ different.

    KILLS: ``random.seed(N)`` at the top of the planner with module-global
    ``random.randint(...)`` in the body. That implementation passes the
    equality half — both calls re-seed the same global stream — and looks
    completely deterministic in a single-run smoke test. It fails the
    INEQUALITY half, because the injected ``rng`` is ignored and
    ``SEED_RANDOM_SEED`` therefore does nothing at all.

    Also kills a planner that returns a constant, and kills a module-level
    ``random.seed(N)`` executed once at import (which dies on the first
    assertion instead, once a second call has advanced the global stream).
    """
    same_a = seed.plan_transactions(FAR_ANCHOR, random.Random(7))
    same_b = seed.plan_transactions(FAR_ANCHOR, random.Random(7))
    other = seed.plan_transactions(FAR_ANCHOR, random.Random(8))

    assert same_a == same_b, "same anchor and seed must produce the same dataset"
    assert same_a != other, (
        "a different SEED_RANDOM_SEED produced an identical dataset, so the "
        "injected rng is being ignored — this is the global-random.seed() bug"
    )


def test_plan_transactions_is_stable_across_repeated_draws_in_one_process():
    """Three consecutive calls, same inputs, same result.

    KILLS: a planner holding module-level RNG state that advances between
    calls — the failure mode a single ``a == b`` comparison misses when the
    implementation happens to reset on the first call only.
    """
    plans = [seed.plan_transactions(FAR_ANCHOR, random.Random(11)) for _ in range(3)]
    assert plans[0] == plans[1] == plans[2]


# ── The anchor must be the only clock ────────────────────────────────────


def test_planners_never_read_the_wall_clock(monkeypatch):
    """Both planners work with ``date.today()`` booby-trapped, AND name no clock.

    ⚠ The monkeypatch alone is weak: it only intercepts a module-global ``date``
    lookup, so it is blind to ``datetime.now().date()``, ``import datetime`` +
    ``datetime.date.today()``, a module-level ``_TODAY`` computed at import
    (already bound before the patch installs), and a ``_today=date.today()``
    default argument evaluated at def time. It is kept as cheap insurance, but
    the source check below is what actually pins the property, alongside the
    FAR_ANCHOR span assertions and the anchor sweep.

    KILLS: any clock read inside either planner, by any spelling.
    """
    monkeypatch.setattr(seed, "date", _ExplodingDate)
    seed.plan_billing_periods(FAR_ANCHOR)
    seed.plan_transactions(FAR_ANCHOR, random.Random(3))

    tree = _seed_tree()
    planners = {"plan_billing_periods", "plan_transactions"}
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in planners:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"today", "now", "utcnow"}, (
                    f"{fn.name} reads the wall clock via .{node.func.attr}() at "
                    f"line {node.lineno}; the anchor must be its only clock"
                )
        # A default argument is evaluated once, at def time — invisible to both
        # the monkeypatch and a naive body scan.
        for default in fn.args.defaults + fn.args.kw_defaults:
            if isinstance(default, ast.Call):
                assert not (
                    isinstance(default.func, ast.Attribute)
                    and default.func.attr in {"today", "now", "utcnow"}
                ), f"{fn.name} has a clock-reading default argument"


def test_plan_transactions_spans_the_anchors_four_months():
    """Every transaction lands in the anchor's month or the three before it.

    KILLS: an off-by-one in the ``range(3, -1, -1)`` month walk, and any
    implementation deriving the span from the wall clock (FAR_ANCHOR is in a
    different year from any plausible run date, so a today-reading planner
    produces months that fail this outright).
    """
    plan = seed.plan_transactions(FAR_ANCHOR, random.Random(5))
    assert plan, "the planner produced no transactions at all"

    months = {(item["date"].year, item["date"].month) for item in plan}
    expected = {(2026, 12), (2027, 1), (2027, 2), (2027, 3)}
    # EQUALITY, not `<=`. A subset check only kills the widening direction:
    # `range(2, -1, -1)` silently drops the oldest month and every other
    # assertion in this file still holds, so a seed producing three months
    # instead of four would ship green. Equality holds at any anchor because
    # each month carries fixed expenses on days 1/3/5.
    assert months == expected, (
        f"month span wrong: missing {expected - months}, unexpected {months - expected}"
    )

    assert max(item["date"] for item in plan) <= FAR_ANCHOR, (
        "the planner emitted a transaction dated after the anchor"
    )


def test_transfers_use_a_category_that_accepts_both_types():
    """Transfers must name a ``CategoryType.BOTH`` category.

    KILLS: reverting to ``general_savings`` / ``investments``, which is what
    the seed used for as long as the rule has existed. ``transaction_service``
    rejects a transfer whose category is not ``BOTH`` with 400 "Transfer
    category must accept both income and expense", and the seeded catalog has
    exactly two such categories: ``transfer`` and ``credit_card_payment``.

    The failure was invisible: the transfer posts checked no status, so every
    transfer 400'd and produced nothing while ``tx_count`` counted it anyway —
    the summary line claimed 117 transactions over 107 real rows. Measured
    against a live stack before the fix: ``linked_transaction_id IS NOT NULL``
    returned 0 rows.

    ⚠ The BOTH set is DERIVED from the real catalog, not hardcoded. Hardcoding
    ``{"transfer", "credit_card_payment"}`` would record the item without
    recording the path: if ``transfer``'s type were ever narrowed away from
    BOTH, or the slug renamed, the seed would go back to 400-ing on every
    transfer while this fence stayed green. (Adding a NEW both-typed category
    is harmless either way — this is a set difference, so a wider allowed set
    is only more permissive.)
    """
    both_typed = {
        c["slug"] for c in org_bootstrap_service.STANDALONE_SYSTEM_CATEGORIES
        if c["type"] is CategoryType.BOTH
    }
    assert both_typed, (
        "no CategoryType.BOTH categories found in the catalog — the derivation "
        "has stopped matching, so this fence would pass vacuously"
    )

    plan = seed.plan_transactions(FAR_ANCHOR, random.Random(5))
    transfers = [i for i in plan if i["kind"] == "transfer"]
    assert transfers, "the planner produced no transfers at all"
    # Both transfer kinds must survive, not just one.
    assert {i["desc"] for i in transfers} >= {"Monthly savings", "ETF investment"}, (
        f"expected both transfer kinds, got {sorted({i['desc'] for i in transfers})}"
    )
    offenders = {i["cat"] for i in transfers} - both_typed
    assert not offenders, (
        f"transfer categories that are not CategoryType.BOTH: {sorted(offenders)}. "
        f"These 400 at the API and silently produce no rows. Allowed: {sorted(both_typed)}"
    )


def test_current_month_variable_draws_are_dense_not_filtered_away():
    """The ``anchor.day`` cap controls DENSITY, so density is what to assert.

    ⚠ This test previously asserted ``max(current).day <= anchor.day`` and
    claimed to kill "dropping the anchor.day cap". It could not: every variable
    expense is already gated by ``if tx_date <= anchor``, so a mutant with an
    unconditional ``day_cap = 28`` simply has most of its draws filtered out and
    the max day stays within range either way. Measured at anchor 2027-03-09
    with ``Random(5)``: unmutated max day 9 / 17 current-month rows, mutated max
    day 5 / 10 rows — both satisfying the old assertion. It was vacuous.

    What the cap actually does is compress every draw into the elapsed part of
    the month, so ALL ``num_var`` (10-18) current-month draws survive. Without
    it only about ``anchor.day / 28`` of them do.

    KILLS: dropping the cap — the sparse current month is most of what this
    dataset exists to demo.
    """
    anchor = date(2027, 3, 9)
    plan = seed.plan_transactions(anchor, random.Random(5))
    variable_descs = {e["desc"] for e in seed.VARIABLE}
    current_var = [
        i for i in plan
        if i["desc"] in variable_descs and (i["date"].year, i["date"].month) == (2027, 3)
    ]
    # 10 is the floor of rng.randint(10, 18). Without the cap the expected
    # survivor count at day 9 is roughly 9/28 of that, i.e. 3-6.
    assert len(current_var) >= 10, (
        f"only {len(current_var)} current-month variable transactions survived; "
        "the anchor.day cap on day_cap has been dropped, so most draws are "
        "landing after the anchor and being filtered out"
    )
    assert max(i["date"] for i in plan) <= anchor


# ── Billing-period geometry: the class-2 date bomb ───────────────────────


def _sweep_anchors():
    """Every day of a 28-day Feb, a leap Feb, and 30- and 31-day months.

    A "full month of anchor dates" is not enough. Sweeping days within ONE
    month cannot reach the ``min(day, 28)`` clamps, which only bite in
    February — so a single-month sweep would miss a boundary expression that
    raises ``ValueError: day is out of range for month``.
    """
    for year, month, days in [
        (2026, 1, 31), (2026, 2, 28), (2028, 2, 29),
        (2026, 4, 30), (2026, 7, 31), (2026, 12, 31),
    ]:
        for day in range(1, days + 1):
            yield date(year, month, day)


def test_billing_period_chain_is_sound_at_every_anchor():
    """Contiguous, gap-free, overlap-free, and never starting in the future.

    KILLS, at specific anchors rather than in principle:

    * dropping ``.replace(day=1)`` from any of the six boundary expressions —
      a one-token slip in an expression that appears six times. Green for
      anchors on the 1st, red on every other day-of-month;
    * relaxing the closed-period predicate from ``end < anchor`` to
      ``end <= anchor`` in one of the two places it is used but not the
      other, which lands the open period exactly on the last closed end;
    * computing ``current_start`` as ``anchor`` or ``anchor.replace(day=25)``.
      For any anchor with day <= 24 the correct open start is the 24th of the
      PREVIOUS month, which is counter-intuitive enough that a reimplementation
      gets it wrong.
    """
    for anchor in _sweep_anchors():
        closed, current_start = seed.plan_billing_periods(anchor)

        # ⚠ ABSOLUTE pin on the first period's start. Without it the first
        # boundary is completely unconstrained: everything else here checks
        # `start <= end`, `end < anchor`, contiguity BETWEEN consecutive pairs,
        # and `current_start` — none of which ever looks at `closed[0][0]`.
        # Mutating `timedelta(days=24)` to `timedelta(days=0)` in the first
        # definition is a 24-day geometry error that passes every other
        # assertion at all 180 anchors. Phrased as the documented property
        # ("salary on the 25th") rather than as the arithmetic.
        assert closed[0][0].day == 25, (
            f"{anchor}: first period starts on day {closed[0][0].day}, expected the 25th"
        )

        for start, end in closed:
            assert start <= end, f"{anchor}: period {start}..{end} ends before it starts"
            assert end < anchor, f"{anchor}: closed period {start}..{end} has not ended yet"
            # A monthly cycle. Pins all six boundary expressions against a
            # one-sided slip without restating any of them.
            assert 27 <= (end - start).days <= 32, (
                f"{anchor}: period {start}..{end} spans {(end - start).days} days, "
                "which is not a monthly cycle"
            )

        for (a_start, a_end), (b_start, _) in zip(closed, closed[1:]):
            assert b_start == a_end + timedelta(days=1), (
                f"{anchor}: gap or overlap between {a_end} and {b_start}"
            )

        if closed:
            assert current_start == closed[-1][1] + timedelta(days=1), (
                f"{anchor}: open period does not start the day after the last closed end"
            )

        assert current_start <= anchor, (
            f"{anchor}: open period starts in the future ({current_start})"
        )


def test_billing_period_regimes_are_the_two_documented_ones():
    """Day 1-24 ⇒ 2 closed periods; day 25-31 ⇒ 3. Pinned, not fixed.

    This regime split IS the class-2 wall-clock bomb the ticket names: the
    shape of the dataset swings on day-of-month, invisibly to grep, and only
    a sweep reveals it. Once the anchor is injectable both regimes are fully
    deterministic, so this is pinned as a documented property rather than
    "fixed" — flattening it is a dataset-shape preference, not a correctness
    matter, and would be a separate decision.

    KILLS: any change to the third period definition's end date (the 24th of
    the anchor's month), which is what the ``end < anchor`` filter turns on.
    """
    for anchor in _sweep_anchors():
        closed, current_start = seed.plan_billing_periods(anchor)
        if anchor.day <= 24:
            assert len(closed) == 2, f"{anchor}: expected 2 closed periods, got {len(closed)}"
            assert current_start.day == 24
            assert current_start < anchor.replace(day=1), (
                f"{anchor}: open period should start in the previous month"
            )
        else:
            assert len(closed) == 3, f"{anchor}: expected 3 closed periods, got {len(closed)}"
            assert current_start == anchor.replace(day=25)


# ── Env resolution must fail loudly, never fall back ─────────────────────


def test_unset_anchor_is_today(monkeypatch):
    """KILLS: pinning the DEFAULT anchor to a literal.

    A fixed default would rot — ``ensure_future_periods`` anchors its stubs to
    the open period's start, so a permanently-past anchor hands every developer
    an org whose open period is months behind the calendar, and the
    current-month branch of the planner goes structurally dead so the
    credit-card pending state is never demoed. The absence of a literal here is
    load-bearing, which is why it is asserted.
    """
    monkeypatch.delenv("SEED_ANCHOR_DATE", raising=False)
    assert seed.resolve_anchor() == date.today()


def test_pinned_anchor_is_honoured(monkeypatch):
    monkeypatch.setenv("SEED_ANCHOR_DATE", "2027-03-17")
    assert seed.resolve_anchor() == FAR_ANCHOR


@pytest.mark.parametrize("bad", ["2026-13-99", "17/03/2027", "today", "2026-02-30", "  "])
def test_malformed_anchor_raises_rather_than_falling_back(bad, monkeypatch):
    """KILLS: ``except ValueError: return date.today()``.

    A silent fallback is the very defect this ticket exists to remove,
    reintroduced by the fix for it: a caller that believes it pinned an anchor
    while actually running on the wall clock. Note ``"  "`` must also raise —
    a whitespace-only value is a typo, not "unset".
    """
    monkeypatch.setenv("SEED_ANCHOR_DATE", bad)
    with pytest.raises(ValueError, match="SEED_ANCHOR_DATE"):
        seed.resolve_anchor()


def test_unset_rng_seed_is_the_documented_default(monkeypatch):
    monkeypatch.delenv("SEED_RANDOM_SEED", raising=False)
    assert seed.resolve_rng().random() == random.Random(seed.DEFAULT_RANDOM_SEED).random()


def test_pinned_rng_seed_is_honoured(monkeypatch):
    """KILLS: ``int(raw)`` then ``return random.Random(DEFAULT_RANDOM_SEED)``.

    That implementation parses the value, raises correctly on garbage, and then
    throws the parsed value away — so ``SEED_RANDOM_SEED=42`` is a complete
    no-op end to end. Without this test it is green across the entire file,
    because every other RNG assertion either passes its own ``Random`` in
    directly or only checks the UNSET default. It is the most realistic wrong
    implementation this ticket can ship.
    """
    monkeypatch.setenv("SEED_RANDOM_SEED", "42")
    assert seed.resolve_rng().random() == random.Random(42).random()
    assert seed.resolve_rng().random() != random.Random(seed.DEFAULT_RANDOM_SEED).random()


def test_resolve_rng_returns_an_instance_and_leaves_the_global_stream_alone(monkeypatch):
    """KILLS: ``random.seed(N); return random`` — returning the MODULE.

    ``resolve_rng``'s docstring states that returning a dedicated instance
    rather than reseeding the global stream is a deliberate design decision,
    because a global reseed is action-at-a-distance for anything that imports
    this module. That property had no coverage: the module-returning
    implementation satisfies every other assertion in this file.
    """
    monkeypatch.delenv("SEED_RANDOM_SEED", raising=False)
    random.seed(999)
    expected = random.random()

    random.seed(999)
    rng = seed.resolve_rng()
    assert isinstance(rng, random.Random)
    assert random.random() == expected, "resolve_rng reseeded the global random stream"


def test_malformed_rng_seed_raises(monkeypatch):
    monkeypatch.setenv("SEED_RANDOM_SEED", "not-a-number")
    with pytest.raises(ValueError, match="SEED_RANDOM_SEED"):
        seed.resolve_rng()


# ── Source-level guards ──────────────────────────────────────────────────


def _seed_tree() -> ast.Module:
    return ast.parse(SEED_PY.read_text(encoding="utf-8"), filename=str(SEED_PY))


def test_main_is_wired_to_the_resolvers_and_reads_no_clock():
    """The feature's ON-SWITCH. Everything else here tests the planners.

    ⚠ Without this, ``main()`` could call ``plan_transactions(date.today(),
    random.Random())`` and every other test in this file would stay green while
    ``SEED_ANCHOR_DATE`` and ``SEED_RANDOM_SEED`` became purely decorative. The
    planners are pure and well fenced; the path from environment to planner was
    not fenced at all.

    KILLS: unwiring either resolver, and any wall-clock read inside ``main``.
    """
    tree = _seed_tree()
    main_fn = next(
        fn for fn in tree.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == "main"
    )

    called = {
        node.func.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"resolve_anchor", "resolve_rng"} <= called, (
        f"main() does not call both resolvers (found {sorted(called)}); "
        "the env vars would be decorative"
    )
    assert {"plan_transactions", "plan_billing_periods"} <= called, (
        "main() does not call both planners"
    )

    # No clock reads anywhere in main: the anchor is the only clock.
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"today", "now", "utcnow"}, (
                f"main() reads the wall clock via .{node.func.attr}() at line "
                f"{node.lineno}; every date must derive from the resolved anchor"
            )

    # The planners must receive the RESOLVED values, not fresh ones.
    for node in ast.walk(main_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"plan_transactions", "plan_billing_periods"}
        ):
            arg_names = {a.id for a in node.args if isinstance(a, ast.Name)}
            assert arg_names, (
                f"{node.func.id} at line {node.lineno} is called with a literal or "
                "expression rather than the resolved anchor/rng names"
            )
            assert "anchor" in arg_names, (
                f"{node.func.id} at line {node.lineno} does not receive `anchor`"
            )


def test_plan_transactions_survives_every_anchor_in_the_sweep():
    """The planner swept across months, not just the two March 2027 anchors.

    ⚠ Every other planner test pins March 2027 on a day <= 24. That leaves no
    February anchor, no day 29-31, and no year boundary — so nothing proves
    ``m_start.replace(day=...)`` cannot raise ``ValueError: day is out of range
    for month``, which is the failure mode the ``min(day, 28)`` clamps exist to
    prevent.

    KILLS: removing any of those clamps, and any future ``day`` value above 28
    added to ``MONTHLY_FIXED`` without one.
    """
    both_typed = {
        c["slug"] for c in org_bootstrap_service.STANDALONE_SYSTEM_CATEGORIES
        if c["type"] is CategoryType.BOTH
    }
    for anchor in _sweep_anchors():
        plan = seed.plan_transactions(anchor, random.Random(4))
        assert plan, f"{anchor}: planner produced nothing"
        assert max(i["date"] for i in plan) <= anchor, (
            f"{anchor}: planner emitted a future-dated transaction"
        )
        months = {(i["date"].year, i["date"].month) for i in plan}
        assert len(months) == 4, f"{anchor}: expected 4 months, got {sorted(months)}"
        for i in plan:
            if i["kind"] == "transfer":
                assert i["cat"] in both_typed, (
                    f"{anchor}: transfer uses non-BOTH category {i['cat']!r}"
                )


def test_seed_writes_nothing_to_the_database_out_of_band():
    """Only ``ensure_verified`` may touch the DB directly.

    KILLS: a ``reset_org()`` / ``_truncate()`` helper added to make
    "deterministic" mean "idempotent". That is a short and tempting path —
    ``async_session`` is already imported and ``ensure_verified`` stands right
    there as a working example of raw SQL — and the seed compounds on re-run,
    so the temptation is real rather than hypothetical.

    Worded on the MECHANISM (``async_session`` / ``text(``) rather than on
    intent, so the "a DELETE is not writing seed *data*" reading cannot slip
    past ``ensure_verified``'s own stated ban.
    """
    tree = _seed_tree()
    offenders = set()
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            uses_session = isinstance(node, ast.Name) and node.id == "async_session"
            uses_text = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "text"
            )
            if uses_session or uses_text:
                offenders.add(fn.name)

    assert offenders == {"ensure_verified"}, (
        f"out-of-band database access outside ensure_verified: "
        f"{sorted(offenders - {'ensure_verified'})}. Seed DATA must go through "
        "the API; a clean slate is `./pfv reset`, not a wipe inside seed.py."
    )


def test_every_seed_env_var_shares_the_prefix():
    """KILLS: ``os.getenv("ANCHOR_DATE")`` — a name the ``pfv`` passthrough drops.

    ``cmd_seed`` forwards every ``SEED_*`` var generically. A var named outside
    that prefix is silently invisible to the documented ``./pfv seed`` path,
    which is the exact failure this ticket's own new variables would otherwise
    have shipped with.
    """
    def _is_environ(node: ast.AST) -> bool:
        """`os.environ` or a bare `environ`."""
        return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
            isinstance(node, ast.Name) and node.id == "environ"
        )

    tree = _seed_tree()
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = node.func
            # os.getenv("X") or a bare getenv("X") after `from os import getenv`
            is_getenv = (isinstance(fn, ast.Attribute) and fn.attr == "getenv") or (
                isinstance(fn, ast.Name) and fn.id == "getenv"
            )
            # os.environ.get("X") — the commonest alternative spelling, and one
            # a `getenv`-only matcher is completely blind to. ⚠ Scoped to an
            # `environ` receiver: matching bare `.get` swallows every
            # `client.get("/api/v1/...")` and `item.get("kind")` in the file.
            is_environ_get = (
                isinstance(fn, ast.Attribute) and fn.attr == "get" and _is_environ(fn.value)
            )
            if (is_getenv or is_environ_get) and isinstance(node.args[0].value, str):
                names.append(node.args[0].value)
        # os.environ["X"] — a subscript, not a call at all
        if (
            isinstance(node, ast.Subscript)
            and _is_environ(node.value)
            and isinstance(node.slice, ast.Constant)
        ):
            names.append(node.slice.value)

    assert len(names) >= 6, (
        f"expected at least the six original SEED_* reads, found {len(names)}: "
        f"{names}. The matcher has stopped matching — check for a spelling it "
        "does not cover before trusting this guard."
    )
    bad = [n for n in names if not n.startswith("SEED_")]
    assert not bad, f"env vars outside the SEED_ prefix are dropped by ./pfv seed: {bad}"


def test_pfv_forwards_seed_vars_generically():
    """The PATH the prefix rule depends on, not just the item.

    ``test_every_seed_env_var_shares_the_prefix`` is only meaningful because
    ``cmd_seed`` forwards every ``SEED_*`` var generically. Restore the old
    six-name enumeration and ``SEED_ANCHOR_DATE`` is silently dropped on the
    command-line path again — with that test still green. This asserts the
    mechanism exists.

    KILLS: reverting ``pfv``'s passthrough to an explicit list.
    """
    # Two locations, deliberately. On a bare CI runner the whole repo is
    # checked out and `pfv` sits at the repo root, two levels above this file.
    # Inside the backend container only /app exists, so the repo-root script is
    # bind-mounted separately — the same both-paths shape
    # test_await_test_run_gate.py uses for the CI gate script.
    candidates = [
        Path(__file__).resolve().parents[2] / "pfv",   # bare checkout / CI
        Path("/app/repo-pfv"),                          # backend container
    ]
    found = [p for p in candidates if p.exists()]
    assert found, (
        f"`pfv` not found at any of {[str(c) for c in candidates]}. It must be "
        "reachable for this guard to mean anything; do not weaken this to a skip."
    )
    pfv = found[0].read_text(encoding="utf-8")
    start = pfv.index("cmd_seed()")
    body = pfv[start:pfv.index("\n}", start)]
    assert "SEED_" in body and ("env |" in body or "environ" in body), (
        "cmd_seed no longer sweeps the environment for SEED_* generically; a "
        "new SEED_* var would be dropped before it reaches seed.py"
    )
    enumerated = body.count("-e SEED_")
    assert enumerated == 0, (
        f"cmd_seed enumerates {enumerated} SEED_* var(s) by name. That is the "
        "defect this ticket removed: any var not on the list is silently "
        "dropped on the command-line path."
    )


def test_accounts_are_seeded_with_opening_balance_not_balance():
    """KILLS: reverting to the ``"balance"`` key.

    ``AccountCreate`` has no ``balance`` field and no ``extra="forbid"``, so
    ``"balance"`` is silently DROPPED by pydantic and ``opening_balance`` falls
    back to ``Decimal("0.00")``: every seeded account is created at zero while
    the script prints the intended amount. Nothing raises, nothing warns, and
    the dataset is wrong in a way no amount assertion downstream could explain.
    """
    payloads = []
    for node in ast.walk(_seed_tree()):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        if "account_type_id" in keys:
            payloads.append(keys)

    # ⚠ Sentinel. Without it, building the payload any other way (dict(...),
    # or `json={**base, **overrides}`) leaves no matching ast.Dict and this
    # guard silently covers nothing while staying green.
    assert payloads, (
        "no account-create payload dict found in seed.py — the parse target "
        "moved, so this guard is no longer checking anything"
    )
    for keys in payloads:
        assert "balance" not in keys, (
            "the account-create payload passes a bare `balance` key, which "
            "AccountCreate silently drops — use `opening_balance`"
        )
        assert "opening_balance" in keys, (
            "the account-create payload does not set `opening_balance`; every "
            "seeded account would open at the 0.00 default"
        )
        # ⚠ The other half. `opening_balance_date` is server_default
        # CURRENT_DATE, so omitting it puts the account's opening row on the RUN
        # date rather than the anchor: determinism breaks, and the Net Worth
        # report (which buckets the opening-balance stream by this column)
        # drops every opening balance out of the generated window. This was
        # inert until `opening_balance` started carrying a non-zero value —
        # fixing one half is what arms the other.
        assert "opening_balance_date" in keys, (
            "the account-create payload does not set `opening_balance_date`; it "
            "would default to the run date, breaking determinism and bucketing "
            "the whole opening balance outside the seeded window in Net Worth"
        )
