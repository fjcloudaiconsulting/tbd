"""TBD-234a — the billing-period anomaly kernel.

Spec: ``specs/2026-07-29-billing-period-roster-design.md`` (revision 5),
§2.2 (the complete roster and the derived end), §2.3 (the status
partition), §2.4 / §2.4a (the anomaly rules and the analysis cap),
§2.5's kernel types, and §4a's test plan (tests 1-14, 14a).

⚠ **Dates here are FULLY FIXED calendar literals, including every injected
``today``, and that REVERSES the house rule** (``reference_wall_clock_date_bomb_tests``).
Spec §4: 234a has no wall clock — §8.1 item 3 makes ``today`` a required
injected argument — so relative anchoring buys nothing and actively hurts.
It is what let round 4's finding F4 hide: a relatively-anchored open row
sits near ``today``, which is exactly where an in-kernel
``max(end, today)`` floor is a no-op. 234b keeps the relative rule,
because its route resolves a real ``date.today()``.

Fixture plumbing: ``backend/tests/conftest.py`` carries no DB fixture and
there is no ``tests/services/conftest.py``, so the ``session_factory``
block below is copied from ``tests/services/test_billing_service.py:38-52``
(spec §4a). Tests 1-10, 12 and 13a need no session; tests 11, 13, 14's
load clause and 14a do.

The ``CompleteRoster`` construction site guard (test 14's AST half) lives
in ``backend/tests/test_complete_roster_single_construction_site.py``,
matching the placement of the two shipped source guards it is modelled on.
Hand-built rosters in this file are legal precisely because that scan is
source-scoped to ``backend/app/`` (spec §2.2, finding F3).
"""
from __future__ import annotations

import datetime
import types

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services import billing_service
from app.services.billing_service import (
    CompleteRoster,
    PeriodAnomaly,
    RosterRow,
    find_period_anomalies,
    kernel_derived_end,
    load_complete_roster,
    period_status,
)


D = datetime.date


@pytest_asyncio.fixture
async def session_factory():
    """In-memory SQLite shared across sessions via StaticPool."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ── Helpers ────────────────────────────────────────────────────────────────


def _roster(*rows: tuple[int, datetime.date, datetime.date | None]) -> CompleteRoster:
    """Hand-build a ``CompleteRoster`` from ``(id, start, end)`` triples.

    Legal in tests and ONLY in tests: the construction-site guard is
    source-scoped to ``backend/app/`` (spec §2.2, F3). Rows are passed in
    ``start_date`` ASC order, the order ``load_complete_roster`` returns.
    """
    return CompleteRoster(
        org_id=1,
        rows=tuple(RosterRow(id=i, start_date=s, end_date=e) for i, s, e in rows),
    )


def _kinds(anomalies: list[PeriodAnomaly]) -> list[str]:
    return [a.kind for a in anomalies]


_STRUCTURAL = frozenset(
    {"gap", "overlap", "duplicate_open", "no_open", "inverted", "straddling"}
)


def _structural(anomalies: list[PeriodAnomaly]) -> list[PeriodAnomaly]:
    """The clock-free half of the output (spec §2.4's two output sets)."""
    return [a for a in anomalies if a.kind in _STRUCTURAL]


def _of_kind(anomalies: list[PeriodAnomaly], kind: str) -> list[PeriodAnomaly]:
    return [a for a in anomalies if a.kind == kind]


async def _seed_periods(
    factory: async_sessionmaker[AsyncSession],
    org_id: int,
    specs: list[tuple[datetime.date, datetime.date | None]],
) -> list[int]:
    """Insert ``specs`` as ``BillingPeriod`` rows; return their ids in order.

    Writes the rows DIRECTLY, bypassing every writer §2.3 enumerates as
    non-inverting, which is what makes the inverted-row fixtures legitimate.
    """
    async with factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one_or_none()
        if org is None:
            db.add(Organization(id=org_id, name=f"org-{org_id}", billing_cycle_day=1))
            await db.commit()
        rows = [BillingPeriod(org_id=org_id, start_date=s, end_date=e) for s, e in specs]
        db.add_all(rows)
        await db.commit()
        return [r.id for r in rows]


# ── Test 1 [guard] ─────────────────────────────────────────────────────────


def test_1_clean_contiguous_roster_has_no_anomalies():
    """Test 1 — ``find_period_anomalies`` on a healthy contiguous roster."""
    roster = _roster(
        (10, D(2026, 1, 1), D(2026, 1, 31)),
        (11, D(2026, 2, 1), D(2026, 2, 28)),
        (12, D(2026, 3, 1), None),
    )

    assert find_period_anomalies(roster, today=D(2026, 3, 15)) == []


# ── Test 2 [guard] ─────────────────────────────────────────────────────────


def test_2_healthy_stub_shape_has_no_structural_anomalies():
    """Test 2 — the intended shape ``[…closed…, OPEN, stub, stub]``.

    Spec §4a offers two ways to scope this: assert the STRUCTURAL set only,
    or pin the fixture converged against the injected ``today``. The first
    is taken deliberately, and the fixture is pinned **lapsed** instead:
    that makes the assertion red against a duplicated, floored
    (``max(end, today)``) end derivation inside ``find_period_anomalies``,
    which test 11 cannot see because ``kernel_derived_end`` takes no clock.
    Under the floor, the open row's end reaches today and paints phantom
    overlaps against both stubs — verbatim the F4 failure.

    It is a ``guard`` for its stated purpose (round 4's F2 proved both of
    revision 4's claimed red conditions pass it), and it is the test that
    caught F1(a)'s self-straddle.
    """
    roster = _roster(
        (20, D(2026, 1, 1), D(2026, 1, 31)),
        (21, D(2026, 2, 1), D(2026, 2, 28)),
        (22, D(2026, 3, 1), None),  # OPEN, interior, lapsed against `today`
        (23, D(2026, 4, 1), D(2026, 4, 30)),  # stub
        (24, D(2026, 5, 1), D(2026, 5, 31)),  # stub
    )
    today = D(2026, 7, 29)

    anomalies = find_period_anomalies(roster, today=today)

    # The open row IS lapsed relative to `today`, so the floored variant has
    # somewhere to reach.
    assert kernel_derived_end(roster, 2) == D(2026, 3, 31)
    assert kernel_derived_end(roster, 2) < today

    assert _structural(anomalies) == []
    assert _kinds(anomalies) == ["lapsed_open"]


# ── Test 3 [fence] ─────────────────────────────────────────────────────────


def test_3_gap_between_two_closed_rows_pins_both_dates():
    """Test 3 — one ``gap``, both bounds pinned per §2.5.

    Red against revision 1's derivation (``successor.start − 1`` for every
    row), which collapses the gap predicate to ``successor.start >
    successor.start`` and emits nothing.
    """
    roster = _roster(
        (30, D(2026, 1, 1), D(2026, 1, 31)),
        (31, D(2026, 3, 1), D(2026, 3, 31)),
        (32, D(2026, 4, 1), None),
    )

    assert find_period_anomalies(roster, today=D(2026, 4, 15)) == [
        PeriodAnomaly(
            kind="gap",
            from_period_id=30,
            to_period_id=31,
            from_date=D(2026, 2, 1),
            to_date=D(2026, 2, 28),
        )
    ]


# ── Test 4 [fence] ─────────────────────────────────────────────────────────


def test_4_overlap_between_two_closed_rows_pins_both_dates():
    """Test 4 — one ``overlap``, dates pinned to ``(rows[j].start_date,
    effective_end(rows, i))`` — the LEFT row's end, never the intersection.

    ⚠ The fixture NESTS ``41`` inside ``40`` on purpose. With two merely
    crossing rows the intersection's upper bound and the left row's end are
    the same date, and the pin proves nothing — a ``min(end_i, end_j)``
    implementation passes. Containment separates them: ``2026-06-30`` is the
    left row's end, ``2026-04-30`` is the intersection's.
    """
    roster = _roster(
        (40, D(2026, 1, 1), D(2026, 6, 30)),
        (41, D(2026, 3, 1), D(2026, 4, 30)),
    )

    assert find_period_anomalies(roster, today=D(2026, 7, 15)) == [
        PeriodAnomaly(
            kind="overlap",
            from_period_id=40,
            to_period_id=41,
            from_date=D(2026, 3, 1),
            to_date=D(2026, 6, 30),
        ),
        PeriodAnomaly(kind="no_open", period_ids=()),
    ]


# ── Test 5 [fence] ─────────────────────────────────────────────────────────


def test_5_overlap_is_all_pairs_not_adjacent_pairs():
    """Test 5 — the nested-containment attack from §2.4.

    ``A`` contains both ``B`` and ``C``; ``B`` and ``C`` abut. An
    adjacent-pair implementation emits ``(A, B)`` alone and renders ``C``
    clean, under-reporting the exact corruption class
    ``routers/settings.py:417-421``'s TOCTOU hole admits.
    """
    roster = _roster(
        (50, D(2026, 1, 1), D(2026, 12, 31)),  # A
        (51, D(2026, 2, 1), D(2026, 2, 28)),  # B
        (52, D(2026, 3, 1), D(2026, 3, 31)),  # C
        (53, D(2027, 1, 1), None),  # keeps `no_open` off the fixture
    )

    overlaps = _of_kind(find_period_anomalies(roster, today=D(2027, 1, 15)), "overlap")

    assert overlaps == [
        PeriodAnomaly(
            kind="overlap",
            from_period_id=50,
            to_period_id=51,
            from_date=D(2026, 2, 1),
            to_date=D(2026, 12, 31),
        ),
        PeriodAnomaly(
            kind="overlap",
            from_period_id=50,
            to_period_id=52,
            from_date=D(2026, 3, 1),
            to_date=D(2026, 12, 31),
        ),
    ]


# ── Test 6 [fence] ─────────────────────────────────────────────────────────


def test_6_duplicate_open_rows_name_both_ids():
    """Test 6 — ``duplicate_open`` carries ids, ASC, derived from the roster.

    There is no ``open_row_ids`` argument: revision 4 deleted that org-wide
    carve-out because org-wide is now the general rule (§2.4).
    """
    roster = _roster(
        (60, D(2026, 1, 1), D(2026, 1, 31)),
        (61, D(2026, 2, 1), None),
        (62, D(2026, 3, 1), None),
    )

    assert find_period_anomalies(roster, today=D(2026, 3, 15)) == [
        PeriodAnomaly(kind="duplicate_open", period_ids=(61, 62))
    ]


# ── Test 7 [fence] ─────────────────────────────────────────────────────────


def test_7_zero_open_rows_emits_no_open_and_computes_no_straddling():
    """Test 7 — the org with closed rows and no open row.

    Round 2's F2: the naive ``straddling`` anchor raises ``AttributeError``
    here, 500ing the page on the exact org it exists for. The marker is not
    computed at all when there is no open row.
    """
    roster = _roster(
        (70, D(2026, 1, 1), D(2026, 1, 31)),
        (71, D(2026, 2, 1), D(2026, 2, 28)),
    )

    anomalies = find_period_anomalies(roster, today=D(2026, 3, 15))

    assert anomalies == [PeriodAnomaly(kind="no_open", period_ids=())]
    assert _of_kind(anomalies, "straddling") == []


# ── Test 8 [fence] ─────────────────────────────────────────────────────────


def test_8_tail_row_is_never_a_left_member_but_is_a_valid_right_member():
    """Test 8 — the tail-row suppression rule, both directions.

    Second clause is red against revision 3's "participates in no pair, on
    either side": both pair rules read only the LEFT row's end, so excluding
    a ``None``-ended row as the RIGHT member suppresses real gaps.
    """
    roster = _roster(
        (80, D(2026, 1, 1), D(2026, 1, 31)),
        (81, D(2026, 3, 1), None),  # tail, open
    )

    anomalies = find_period_anomalies(roster, today=D(2026, 3, 15))

    # The tail row derives no end, so it never opens a pair of its own …
    assert kernel_derived_end(roster, 1) is None
    # … and the genuine gap whose RIGHT member it is IS still reported.
    assert anomalies == [
        PeriodAnomaly(
            kind="gap",
            from_period_id=80,
            to_period_id=81,
            from_date=D(2026, 2, 1),
            to_date=D(2026, 2, 28),
        )
    ]


# ── Test 9 [fence] ─────────────────────────────────────────────────────────


def test_9_non_adjacent_straddler_emits_straddling_and_its_own_overlap():
    """Test 9 — straddling, non-adjacent, with two open rows.

    ``S`` straddles the MAX-start open row ``O``, separated from it by the
    intervening open row ``X``. §2.4's precedence ruling: ``straddling`` is
    emitted **in addition to** ``overlap``, never instead of it.

    ⚠ The id assertion is what makes this non-vacuous. ``overlap(S, X)``
    also holds and an adjacent-pair implementation emits it, so a bare "an
    overlap marker is present" would go green against the very
    implementation all-pairs exists to kill.
    """
    roster = _roster(
        (90, D(2026, 1, 1), D(2026, 12, 31)),  # S — closed straddler
        (91, D(2026, 2, 1), None),  # X — open, interior
        (92, D(2026, 3, 1), None),  # O — open, MAX start → the anchor
    )

    anomalies = find_period_anomalies(roster, today=D(2026, 3, 15))

    assert _of_kind(anomalies, "straddling") == [
        PeriodAnomaly(kind="straddling", period_id=90, anchor_period_id=92)
    ]
    overlaps = _of_kind(anomalies, "overlap")
    assert PeriodAnomaly(
        kind="overlap",
        from_period_id=90,
        to_period_id=92,
        from_date=D(2026, 3, 1),
        to_date=D(2026, 12, 31),
    ) in overlaps
    # The anchor never straddles itself (§2.4 F1(a); the shipped precedent
    # is `_apply_close_step`'s `id != current.id` at billing_service.py:774).
    assert all(a.period_id != 92 for a in _of_kind(anomalies, "straddling"))


# ── Test 10 [fence] ────────────────────────────────────────────────────────


def test_10_inverted_row_is_flagged_and_suppresses_its_pair_rules():
    """Test 10 — ``end_date < start_date``, built directly.

    Not vacuous: the row bypasses every writer §2.3 proves non-inverting,
    which is the only way this shape exists. Without the suppression rule
    the left pair emits a spurious ``overlap`` and the right pair a spurious
    ``gap``, neither of which describes anything a reader can act on.

    ⚠ §2.5's ``length_days`` is a 234b RESPONSE field with no 234a
    deliverable behind it, so only the two kernel-side facts are asserted
    here (see the report's spec-defect note).
    """
    roster = _roster(
        (100, D(2026, 1, 1), D(2026, 2, 15)),
        (101, D(2026, 2, 1), D(2026, 1, 15)),  # INVERTED
        (102, D(2026, 3, 1), D(2026, 3, 31)),
        (103, D(2026, 4, 1), None),
    )
    today = D(2026, 4, 15)

    assert period_status(roster.rows[1], today=today) == "invalid"
    assert find_period_anomalies(roster, today=today) == [
        PeriodAnomaly(kind="inverted", period_id=101)
    ]


# ── Test 11 [fence] — the flagship differential ────────────────────────────


@pytest.mark.asyncio
async def test_11_kernel_derived_end_matches_period_effective_end(session_factory):
    """Test 11 ⭐ — the differential fence.

    For EVERY row: ``kernel_derived_end(roster, i)`` equals
    ``await period_effective_end(db, org_id, row)``. The kernel does not
    call the helper, so this is a genuine differential.

    Fixture clauses (a)-(e) from §4a, all normative:

      (a) a closed row whose ``end_date != successor.start − 1``
          → kills revision 1's ``successor.start − 1``-for-every-row formula
      (b) a closed row whose ``end_date >= successor.start`` (an overlap)
      (c) an open INTERIOR row, asserting ``successor.start − 1``
          → kills an ``effective_end`` that returns ``None`` for open rows
      (d) an open TAIL row, asserting ``None``
      (e) the clause-(c) row is LAPSED against the injected ``today``
          → kills an in-kernel ``max(end, today)`` floor

    Both views are derived from ONE seeded DB — the row tuples from
    ``load_complete_roster``, the ORM instances from a plain ordered
    SELECT — so the test cannot drift into comparing two hand-built
    representations of different rosters. They are index-aligned because
    ``uq_billing_period_org_start`` makes ``start_date`` unique per org,
    and the per-row id assertion is the belt on that.
    """
    org_id = 1
    today = D(2026, 7, 29)
    await _seed_periods(
        session_factory,
        org_id,
        [
            (D(2023, 1, 1), D(2023, 3, 15)),  # (a) end != successor.start − 1
            (D(2023, 4, 1), D(2023, 9, 30)),  # (b) end >= successor.start
            (D(2023, 6, 1), D(2023, 6, 30)),
            (D(2024, 1, 1), None),  # (c) open INTERIOR, (e) lapsed
            (D(2024, 3, 1), D(2024, 3, 31)),
            (D(2025, 1, 1), None),  # (d) open TAIL
        ],
    )

    async with session_factory() as db:
        roster = await load_complete_roster(db, org_id)
        orm_rows = list(
            (
                await db.execute(
                    select(BillingPeriod)
                    .where(BillingPeriod.org_id == org_id)
                    .order_by(BillingPeriod.start_date)
                )
            ).scalars()
        )

        assert len(orm_rows) == len(roster.rows) == 6

        for i, orm in enumerate(orm_rows):
            assert orm.id == roster.rows[i].id  # the index-alignment belt
            assert kernel_derived_end(roster, i) == await billing_service.period_effective_end(
                db, org_id, orm
            ), f"derived end diverged from the oracle at row {i}"

    # The clauses, spelled out so a future fixture edit cannot quietly
    # dissolve them back into a clean contiguous roster.
    assert kernel_derived_end(roster, 0) == D(2023, 3, 15)  # (a) verbatim
    assert roster.rows[1].start_date - datetime.timedelta(days=1) != D(2023, 3, 15)
    assert kernel_derived_end(roster, 1) == D(2023, 9, 30)  # (b) verbatim
    assert kernel_derived_end(roster, 1) >= roster.rows[2].start_date
    assert kernel_derived_end(roster, 3) == D(2024, 2, 29)  # (c) successor − 1
    assert kernel_derived_end(roster, 3) < today  # (e) LAPSED
    assert kernel_derived_end(roster, 5) is None  # (d) tail


# ── Test 12 [fence] ────────────────────────────────────────────────────────


def test_12_status_partition_is_ordered_and_total():
    """Test 12 — §2.3's five branches, first match wins, ``today`` injected.

    The open-row-starting-tomorrow case is what fences the ORDER: an
    implementer who evaluates ``upcoming`` before ``open`` returns
    ``"upcoming"`` for it and produces a different canonical answer while
    still satisfying an unordered reading of the rules.
    """
    today = D(2026, 7, 29)

    invalid = RosterRow(id=120, start_date=D(2026, 2, 1), end_date=D(2026, 1, 15))
    open_tomorrow = RosterRow(id=121, start_date=D(2026, 7, 30), end_date=None)
    current = RosterRow(id=122, start_date=D(2026, 7, 1), end_date=D(2026, 8, 31))
    upcoming = RosterRow(id=123, start_date=D(2026, 9, 1), end_date=D(2026, 9, 30))
    past = RosterRow(id=124, start_date=D(2026, 1, 1), end_date=D(2026, 1, 31))

    assert period_status(invalid, today=today) == "invalid"
    assert period_status(open_tomorrow, today=today) == "open"
    assert period_status(current, today=today) == "current_by_calendar"
    assert period_status(upcoming, today=today) == "upcoming"
    assert period_status(past, today=today) == "past"


# ── Test 13 [fence] ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_13_analysis_cap_suppresses_overlap_alone(session_factory):
    """Test 13 — §2.4a. 2001 rows (``> 2000``, the pinned comparison).

    Overlap analysis is refused and says so; every ``O(n)`` marker still
    runs, so ``duplicate_open`` survives — which is precisely where that
    corruption hides on 1000+ row orgs. The list is never silently empty.

    The rows are SEEDED and routed through ``load_complete_roster``: a pure
    version would construct a ``CompleteRoster`` at a second site, the shape
    test 14's AST guard forbids in ``backend/app/`` and which this file is
    exempt from only by source scoping.
    """
    org_id = 1
    base = D(2020, 1, 1)
    specs: list[tuple[datetime.date, datetime.date | None]] = [
        (base + datetime.timedelta(days=k), base + datetime.timedelta(days=k))
        for k in range(1999)
    ]
    specs.append((base + datetime.timedelta(days=1999), None))  # open, interior
    specs.append((base + datetime.timedelta(days=2000), None))  # open, tail
    open_ids = (await _seed_periods(session_factory, org_id, specs))[-2:]

    async with session_factory() as db:
        roster = await load_complete_roster(db, org_id)

    assert len(roster.rows) == 2001

    anomalies = find_period_anomalies(roster, today=D(2030, 1, 1))

    assert anomalies != []
    assert _of_kind(anomalies, "overlap") == []
    assert _of_kind(anomalies, "overlap_analysis_skipped") == [
        PeriodAnomaly(kind="overlap_analysis_skipped", period_count=2001, cap=2000)
    ]
    assert _of_kind(anomalies, "duplicate_open") == [
        PeriodAnomaly(kind="duplicate_open", period_ids=tuple(open_ids))
    ]


# ── Test 13a [fence] — the emission ceiling ────────────────────────────────


def test_13a_overlap_emission_ceiling_refuses_rather_than_truncating_silently():
    """Test 13a — §2.4a's emission ceiling.

    ⚠ Added here rather than taken from §4a: revision 5 introduced the
    ceiling and its ``overlap_emission_capped`` marker without assigning a
    test to either (see the report). A shipped-but-unfenced emission path is
    the defect class this programme keeps catching, so it gets one.

    101 mutually-containing rows yield 5050 candidate overlaps against a
    ceiling of 5000. Below the analysis cap, so the comparison loop is never
    refused — the marker must come from the ceiling, not from §2.4a's cap.
    """
    rows: list[tuple[int, datetime.date, datetime.date | None]] = [
        (200 + k, D(2020, 1, 1) + datetime.timedelta(days=k), D(2040, 1, 1))
        for k in range(101)
    ]
    roster = _roster(*rows)

    anomalies = find_period_anomalies(roster, today=D(2030, 1, 1))

    assert len(_of_kind(anomalies, "overlap")) == 5000
    assert _of_kind(anomalies, "overlap_emission_capped") == [
        PeriodAnomaly(kind="overlap_emission_capped", emitted_count=5000, cap=5000)
    ]
    assert _of_kind(anomalies, "overlap_analysis_skipped") == []


# ── Test 13b [fence] — §2.5's pinned ordering ──────────────────────────────


def test_13b_anomaly_list_ordering_is_pinned_to_the_declaration_order():
    """Test 13b — §2.5's ordering ruling.

    ⚠ Added here rather than taken from §4a, for the same reason as 13a:
    revision 4 left ordering unspecified, revision 5 pinned it as normative
    and assigned it no test — yet the pin is what licenses tests 3, 4, 6, 7,
    8 and 10 to assert their lists DIRECTLY. An unfenced pin makes six other
    assertions accidentally order-sensitive.

    The fixture emits ``inverted`` first and ``gap`` second in rule order,
    so the two must come back swapped: `gap` (0) then `no_open` (3) then
    `inverted` (4).
    """
    roster = _roster(
        (130, D(2026, 1, 1), D(2026, 1, 31)),
        (131, D(2026, 3, 1), D(2026, 3, 31)),  # gap against 130
        (132, D(2026, 4, 1), D(2026, 2, 15)),  # INVERTED
        (133, D(2026, 5, 1), D(2026, 5, 31)),
    )

    assert find_period_anomalies(roster, today=D(2026, 6, 15)) == [
        PeriodAnomaly(
            kind="gap",
            from_period_id=130,
            to_period_id=131,
            from_date=D(2026, 2, 1),
            to_date=D(2026, 2, 28),
        ),
        PeriodAnomaly(kind="no_open", period_ids=()),
        PeriodAnomaly(kind="inverted", period_id=132),
    ]


# ── Test 14 [fence] — the load half ────────────────────────────────────────


@pytest.mark.asyncio
async def test_14_load_complete_roster_returns_every_row_ascending(session_factory):
    """Test 14 (load clause) — the ONLY constructor returns the WHOLE roster.

    Larger than ``list_periods``' 24-row cap and larger than any lookback
    window 234b will apply, org-scoped, ``start_date`` ASC, no LIMIT and no
    date predicate.

    (The AST half of test 14 lives in
    ``backend/tests/test_complete_roster_single_construction_site.py``.)
    """
    org_id = 1
    other_org_id = 2
    base = D(2020, 1, 1)
    specs: list[tuple[datetime.date, datetime.date | None]] = [
        (base + datetime.timedelta(days=31 * k), base + datetime.timedelta(days=31 * k + 30))
        for k in range(29)
    ]
    specs.append((base + datetime.timedelta(days=31 * 29), None))
    ids = await _seed_periods(session_factory, org_id, specs)
    await _seed_periods(
        session_factory,
        other_org_id,
        [(D(2021, 6, 1), D(2021, 6, 30)), (D(2021, 7, 1), None)],
    )

    async with session_factory() as db:
        roster = await load_complete_roster(db, org_id)

    assert roster.org_id == org_id
    assert len(roster.rows) == 30 > 24
    assert [r.id for r in roster.rows] == ids
    assert [r.start_date for r in roster.rows] == [s for s, _ in specs]
    assert [r.end_date for r in roster.rows] == [e for _, e in specs]
    assert list(roster.rows) == sorted(roster.rows, key=lambda r: r.start_date)
    # Row tuples, not ORM entities (§2.2 amendment 1).
    assert all(isinstance(r, RosterRow) for r in roster.rows)


# ── Test 14a [fence] — clock injection ─────────────────────────────────────


@pytest.mark.asyncio
async def test_14a_kernel_never_consults_date_today(session_factory, monkeypatch):
    """Test 14a — §8.1 item 3. ``today`` is injected and **required**.

    ``find_period_anomalies``, ``kernel_derived_end`` and ``period_status``
    must never fall back to ``date.today()``. Pattern reused verbatim from
    ``tests/services/test_billing_service.py:1400-1425``.

    ⚠ Two halves, and the second one is not decorative. The
    ``_ExplodingDate`` monkeypatch only proves no fallback fires **when
    ``today`` is passed**, so it goes green against a kernel that gives
    ``today`` a ``None`` default and calls ``date.today()`` behind it — the
    signature assertions are what make "required" mean required. They also
    pin ``kernel_derived_end`` as clock-free, which is the frozen contract's
    structural half: a ``today`` parameter there, in any form, is the
    one-line door to ``period_spend_window_end``'s floored semantics (F4).
    """
    import inspect

    kde_params = inspect.signature(billing_service.kernel_derived_end).parameters
    assert list(kde_params) == ["roster", "i"], (
        "kernel_derived_end must take NO clock (§8.1 item 1) — a `today` "
        "parameter here is how the floored end semantics get reimplemented"
    )
    for fn in (billing_service.find_period_anomalies, billing_service.period_status):
        param = inspect.signature(fn).parameters["today"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__}'s `today` must be REQUIRED (§8.1 item 3); a "
            "default is where a date.today() fallback hides"
        )

    org_id = 1
    await _seed_periods(
        session_factory,
        org_id,
        [
            (D(2026, 1, 1), D(2026, 1, 31)),
            (D(2026, 3, 1), None),
        ],
    )
    async with session_factory() as db:
        roster = await load_complete_roster(db, org_id)

    class _ExplodingDate(datetime.date):
        @classmethod
        def today(cls):  # pragma: no cover - must never be reached
            raise AssertionError("date.today() consulted despite an injected today=")

    # Swap only the NAME `datetime` inside billing_service, not the global
    # module: replacing `datetime.date` process-wide breaks SQLAlchemy's
    # isinstance-based Date coercion. `_ExplodingDate` subclasses `date`, so
    # every construction and comparison the module performs still works.
    monkeypatch.setattr(
        billing_service,
        "datetime",
        types.SimpleNamespace(date=_ExplodingDate, timedelta=datetime.timedelta),
    )

    today = D(2026, 3, 15)
    assert billing_service.kernel_derived_end(roster, 0) == D(2026, 1, 31)
    assert billing_service.kernel_derived_end(roster, 1) is None
    assert billing_service.period_status(roster.rows[0], today=today) == "past"
    assert billing_service.find_period_anomalies(roster, today=today) == [
        PeriodAnomaly(
            kind="gap",
            from_period_id=roster.rows[0].id,
            to_period_id=roster.rows[1].id,
            from_date=D(2026, 2, 1),
            to_date=D(2026, 2, 28),
        )
    ]
