"""Wire contract for ``GET /settings/billing-periods/roster`` (TBD-234b).

Spec: ``specs/2026-07-29-billing-period-roster-design.md`` §2.5.

The kernel (TBD-234a, `15faa922`) emits frozen dataclasses and imports no
Pydantic at all; this module is the **wire boundary** over them. Two rules
govern it and both are normative:

* **The nine kinds must not exist in a third hand-written copy.**
  ``billing_service.AnomalyKind`` and ``billing_service._KIND_ORDER`` already
  write them down twice. :data:`ANOMALY_MODELS` is checked against
  ``AnomalyKind`` at import time, so a tenth kind landing in the kernel
  breaks loudly here instead of drifting silently through §2.5's
  tolerate-unknown rule.
* **The dataclass → model mapping is PER-KIND EXPLICIT, never a
  ``dataclasses.asdict`` sweep.** The shipped payloads are not uniform:
  ``no_open`` carries ``period_ids=()`` while **both** refusal markers carry
  ``period_ids=None``. A sweep serialises those two indistinguishably, or
  emits every ``None`` field on every variant — which is the nine-column
  table §1.1 forbids, arriving through the wire format.

Precedent for the discriminated union is ``schemas/report_layout.py`` (nine
widget variants, ``Field(discriminator="type")``) and ``schemas/scenario.py``
— **not** ``schemas/dashboard.py``, which is an INPUT union whose variants
are field-identical and therefore says nothing about a response union whose
variants have genuinely different field sets.
"""
from __future__ import annotations

import datetime
from typing import Annotated, Literal, Union, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.services.billing_service import AnomalyKind, PeriodAnomaly, PeriodStatus


class _AnomalyBase(BaseModel):
    """Every marker carries ``off_window``.

    ⚠ **True when any id the marker references is absent from ``periods``**,
    and therefore vacuously ``false`` on the three roster-scoped kinds
    (``no_open`` carries an empty ``period_ids``; both refusal markers carry
    none at all). The field is emitted for schema uniformity. A client must
    **not** use it to decide whether a roster-scoped marker renders — §1.1's
    third marker class governs those unconditionally, and a band written as
    ``anomalies.filter(a => a.off_window)`` erases ``no_open`` on the exact
    org this page exists for.
    """

    off_window: bool


class GapAnomaly(_AnomalyBase):
    """The UNCOVERED interval itself, both bounds inclusive."""

    kind: Literal["gap"]
    from_period_id: int
    to_period_id: int
    from_date: datetime.date
    to_date: datetime.date


class OverlapAnomaly(_AnomalyBase):
    """``from_date`` is ``rows[j].start_date``; ``to_date`` is the **LEFT**
    row's derived end — not the intersection."""

    kind: Literal["overlap"]
    from_period_id: int
    to_period_id: int
    from_date: datetime.date
    to_date: datetime.date


class DuplicateOpenAnomaly(_AnomalyBase):
    """Every open row's id, ``start_date`` ASC. Ids, never a count."""

    kind: Literal["duplicate_open"]
    period_ids: list[int]


class NoOpenAnomaly(_AnomalyBase):
    """``period_ids`` is always empty; the field is present for schema
    uniformity. Roster-scoped (§1.1)."""

    kind: Literal["no_open"]
    period_ids: list[int]


class InvertedAnomaly(_AnomalyBase):
    kind: Literal["inverted"]
    period_id: int


class StraddlingAnomaly(_AnomalyBase):
    """The straddler and the MAX-start open row it straddles."""

    kind: Literal["straddling"]
    period_id: int
    anchor_period_id: int


class LapsedOpenAnomaly(_AnomalyBase):
    """The anchored open row and its derived end, which is ``< today``."""

    kind: Literal["lapsed_open"]
    period_id: int
    effective_end: datetime.date


class OverlapAnalysisSkippedAnomaly(_AnomalyBase):
    """Roster-scoped (§1.1). ``period_count`` is the org's true row count."""

    kind: Literal["overlap_analysis_skipped"]
    period_count: int
    cap: int


class OverlapEmissionCappedAnomaly(_AnomalyBase):
    """Roster-scoped (§1.1). ``overlap_count`` is what the roster WOULD have
    emitted, always ``> cap`` when this marker fires — never the emitted
    count, which is ``cap`` by construction."""

    kind: Literal["overlap_emission_capped"]
    overlap_count: int
    cap: int


#: kind → model. The union and the import-time coverage check are both
#: derived from this one mapping, so the kinds are never typed a third time.
ANOMALY_MODELS: dict[str, type[_AnomalyBase]] = {
    "gap": GapAnomaly,
    "overlap": OverlapAnomaly,
    "duplicate_open": DuplicateOpenAnomaly,
    "no_open": NoOpenAnomaly,
    "inverted": InvertedAnomaly,
    "straddling": StraddlingAnomaly,
    "lapsed_open": LapsedOpenAnomaly,
    "overlap_analysis_skipped": OverlapAnalysisSkippedAnomaly,
    "overlap_emission_capped": OverlapEmissionCappedAnomaly,
}

if set(ANOMALY_MODELS) != set(get_args(AnomalyKind)):  # pragma: no cover
    # `raise`, not `assert` — bare asserts are stripped under `python -O`,
    # and a silently-uncovered kind serialises through §2.5's
    # tolerate-unknown rule without anything objecting.
    raise RuntimeError(
        "billing_roster's anomaly union has drifted from "
        "billing_service.AnomalyKind: "
        f"{set(get_args(AnomalyKind)) ^ set(ANOMALY_MODELS)}"
    )

RosterAnomaly = Annotated[
    Union[tuple(ANOMALY_MODELS.values())],  # type: ignore[valid-type]
    Field(discriminator="kind"),
]


def to_wire_anomaly(anomaly: PeriodAnomaly, *, off_window: bool) -> _AnomalyBase:
    """One kernel marker → its wire variant. **Per kind, explicitly.**

    Deliberately not a loop over ``dataclasses.fields``: see the module
    docstring for why ``no_open``'s ``()`` and the refusal markers' ``None``
    must not collapse into the same serialisation.
    """
    kind = anomaly.kind
    if kind == "gap" or kind == "overlap":
        return ANOMALY_MODELS[kind](
            kind=kind,
            from_period_id=anomaly.from_period_id,
            to_period_id=anomaly.to_period_id,
            from_date=anomaly.from_date,
            to_date=anomaly.to_date,
            off_window=off_window,
        )
    if kind == "duplicate_open":
        return DuplicateOpenAnomaly(
            kind=kind,
            period_ids=list(anomaly.period_ids or ()),
            off_window=off_window,
        )
    if kind == "no_open":
        return NoOpenAnomaly(
            kind=kind,
            period_ids=list(anomaly.period_ids or ()),
            off_window=off_window,
        )
    if kind == "inverted":
        return InvertedAnomaly(
            kind=kind, period_id=anomaly.period_id, off_window=off_window
        )
    if kind == "straddling":
        return StraddlingAnomaly(
            kind=kind,
            period_id=anomaly.period_id,
            anchor_period_id=anomaly.anchor_period_id,
            off_window=off_window,
        )
    if kind == "lapsed_open":
        return LapsedOpenAnomaly(
            kind=kind,
            period_id=anomaly.period_id,
            effective_end=anomaly.effective_end,
            off_window=off_window,
        )
    if kind == "overlap_analysis_skipped":
        return OverlapAnalysisSkippedAnomaly(
            kind=kind,
            period_count=anomaly.period_count,
            cap=anomaly.cap,
            off_window=off_window,
        )
    if kind == "overlap_emission_capped":
        return OverlapEmissionCappedAnomaly(
            kind=kind,
            overlap_count=anomaly.overlap_count,
            cap=anomaly.cap,
            off_window=off_window,
        )
    # Unreachable while the import-time check above holds.
    raise RuntimeError(f"unmapped anomaly kind {kind!r}")  # pragma: no cover


def anomaly_referenced_ids(anomaly: PeriodAnomaly) -> tuple[int, ...]:
    """Every period id a marker names, in field order then ``period_ids``.

    Drives both ``off_window`` and ``referenced_periods``. Deliberately the
    same field set ``billing_service._anomaly_sort_key`` reads, so a tenth
    kind cannot be ordered by an id this function does not see.
    """
    ids: list[int] = []
    for value in (
        anomaly.from_period_id,
        anomaly.to_period_id,
        anomaly.period_id,
        anomaly.anchor_period_id,
    ):
        if value is not None:
            ids.append(value)
    if anomaly.period_ids:
        ids.extend(anomaly.period_ids)
    return tuple(ids)


class RosterScope(BaseModel):
    """**Org-wide**: the anomaly domain. Never conflated with ``window``."""

    period_count: int
    first_start: datetime.date | None
    last_start: datetime.date | None
    analyzed: bool


class WindowScope(BaseModel):
    """**Display only.**

    ``from`` is the minimum DISPLAYED ``start_date`` — the truncated lower
    bound, not the requested lookback bound — and is ``null`` when
    ``periods`` is empty. ``to`` is **permanently null**: D8 gives the window
    no upper bound so nothing can ever populate it; it is kept for schema
    stability and is dead, so a reader should not go hunting for the code
    that sets it.

    ``months`` is deliberately absent: the page owns the query param it sent.

    ⚠ **Neither nullable field carries a DEFAULT**, and that is deliberate.
    A default makes the field OPTIONAL in the generated OpenAPI schema, so a
    generated TS client types `window.from` as `string | undefined` and a
    consumer can no longer tell "empty display window" — §2.5's legitimate
    maximally-lapsed case — from "field absent". Every other nullable field on
    this wire (``RosterPeriod.end_date``/``effective_end``/
    ``counting_through``/``length_days``, ``ReferencedPeriod.end_date``/
    ``effective_end``) is required-and-nullable; these two now match. The route
    always passes both explicitly, so nothing relies on the default.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: datetime.date | None = Field(alias="from")
    to: datetime.date | None
    displayed_count: int
    truncated: bool


class RosterPeriod(BaseModel):
    """One displayed row. §2.1's two ends are BOTH present, always.

    ``effective_end`` is ``period_effective_end``'s semantics (no clock);
    ``counting_through`` is ``period_spend_window_end``'s (floored at today
    on an open row). Collapsing them is the defect §2.1 exists to prevent —
    ship one number and this page becomes the one that proves the app is
    lying.
    """

    id: int
    start_date: datetime.date
    end_date: datetime.date | None
    effective_end: datetime.date | None
    counting_through: datetime.date | None
    status: PeriodStatus
    length_days: int | None
    transaction_count: int
    #: String, per the repo's Decimal wire convention
    #: (``specs/tech-debt-frontend-decimal-typing.md``).
    settled_net: str


class ReferencedPeriod(BaseModel):
    """One entry of ``referenced_periods``.

    ``effective_end`` is **mandatory**: without it the page cannot render an
    off-window open row's gap bounds without recomputing what the kernel
    already knew.
    """

    id: int
    start_date: datetime.date
    end_date: datetime.date | None
    effective_end: datetime.date | None
    status: PeriodStatus


class RosterResponse(BaseModel):
    """§2.5's response body.

    ``referenced_periods`` is **required, not optional**, and carries one
    entry per id ANY marker names — **in-window ids included**, so a client
    never has to decide which map to read from.
    """

    roster: RosterScope
    window: WindowScope
    periods: list[RosterPeriod]
    anomalies: list[RosterAnomaly]
    referenced_periods: dict[str, ReferencedPeriod]
