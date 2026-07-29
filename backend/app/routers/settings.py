import datetime
from decimal import Decimal

import structlog
from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, case, func, or_, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.billing import BillingPeriod
from app.models.settings import OrgSetting
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.rate_limit import get_client_ip
from app.schemas.billing_roster import (
    ReferencedPeriod,
    RosterPeriod,
    RosterResponse,
    RosterScope,
    WindowScope,
    anomaly_referenced_ids,
    to_wire_anomaly,
)
from app.schemas.settings import (
    BillingCycleUpdate,
    BillingPeriodCreate,
    ManualBalanceAdjustmentResponse,
    ManualBalanceAdjustmentToggle,
    OrgSettingResponse,
    OrgSettingUpdate,
)
from app.services import audit_service, billing_service
from app.services.exceptions import ConflictError, ValidationError
from app.services.transaction_filters import reportable_transaction_filter
from app.services.settings_service import (
    FORECAST_GRANULARITY_VALUES,
    FORECAST_INPUT_GRANULARITY_KEY,
)

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# The "feature." prefix is exclusively managed by superadmin endpoints in
# admin_features.py, which layer per-org OrgSetting overrides at the highest
# priority in the three-level feature gate (feature_gate.py: per-org >
# SystemSetting global > env-floor).  Allowing the generic PUT/DELETE here
# would let any OWNER/ADMIN bypass a globally-disabled feature with no audit
# trail.  Block the entire namespace from this writer.
RESERVED_SETTINGS_PREFIX = "feature."


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware."""
    return structlog.contextvars.get_contextvars().get("request_id")


def _require_admin(user: User) -> None:
    if user.role not in (Role.OWNER, Role.ADMIN) and not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


@router.get("", response_model=list[OrgSettingResponse])
async def list_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    result = await db.execute(
        select(OrgSetting)
        .where(OrgSetting.org_id == current_user.org_id)
        .order_by(OrgSetting.key)
    )
    return [
        OrgSettingResponse(key=s.key, value=s.value) for s in result.scalars().all()
    ]


@router.put("", response_model=OrgSettingResponse)
async def upsert_setting(
    body: OrgSettingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)

    if body.key.startswith(RESERVED_SETTINGS_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The 'feature.' settings namespace is managed by platform administrators",
        )

    # Per-key bounds validation. Other org settings have no bounds
    # contract today; only the session-lifetime key actually drives
    # the session TTL, so an out-of-bounds write here would log
    # users out instantly or hand them a year-long session.
    if body.key == "session_lifetime_days":
        try:
            days = int(body.value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_lifetime_days must be an integer (days)",
            )
        if not (1 <= days <= 365):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_lifetime_days must be between 1 and 365",
            )

    # Forecast build granularity is a closed enum (master|subcategory). The
    # service defends by falling back to master on garbage, but rejecting a
    # bad write here avoids a silently-ignored setting that confuses admins.
    if body.key == FORECAST_INPUT_GRANULARITY_KEY:
        if body.value not in FORECAST_GRANULARITY_VALUES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "forecast_input_granularity must be one of: "
                    f"{', '.join(FORECAST_GRANULARITY_VALUES)}"
                ),
            )

    result = await db.execute(
        select(OrgSetting).where(
            OrgSetting.org_id == current_user.org_id,
            OrgSetting.key == body.key,
        )
    )
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = body.value
    else:
        setting = OrgSetting(
            org_id=current_user.org_id, key=body.key, value=body.value
        )
        db.add(setting)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Concurrent insert won the race — retry as update
        result = await db.execute(
            select(OrgSetting).where(
                OrgSetting.org_id == current_user.org_id,
                OrgSetting.key == body.key,
            )
        )
        setting = result.scalar_one()
        setting.value = body.value
        await db.commit()

    await db.refresh(setting)
    return OrgSettingResponse(key=setting.key, value=setting.value)


@router.delete("/{key}", status_code=204)
async def delete_setting(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)

    if key.startswith(RESERVED_SETTINGS_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The 'feature.' settings namespace is managed by platform administrators",
        )

    result = await db.execute(
        select(OrgSetting).where(
            OrgSetting.org_id == current_user.org_id,
            OrgSetting.key == key,
        )
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        raise HTTPException(status_code=404, detail="Setting not found")

    await db.delete(setting)
    await db.commit()


@router.get("/billing-cycle")
async def get_billing_cycle(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Organization).where(Organization.id == current_user.org_id)
    )
    org = result.scalar_one()
    return {"billing_cycle_day": org.billing_cycle_day}


@router.put("/billing-cycle")
async def update_billing_cycle(
    body: BillingCycleUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Set the org's billing cycle day. Applies from the NEXT period.

    For an org that already has an open period this writes one column and
    modifies no period row and no budget. A cycle-day change is a
    scheduling hint (``billing_service`` module docstring); the org
    migrates onto the new grid at its next close, which ``BillingCloseJob``
    performs automatically when ``automate_billing_close`` is on.

    For an org with NO open period the ``get_current_period`` call below
    inserts one and commits, so "zero rows written" is not true for that
    shape. See the note on that call further down.

    TBD-239 deleted the re-anchor that used to move the open period's
    ``start_date`` in place. It checked only exact ``start_date`` equality,
    never the predecessor's ``end_date``, so a forward move opened a gap
    (days belonging to no period: invisible to ``budget_service.list_budgets``
    and the forecast, yet still counted in the account balance) and a
    backward move silently pulled already-closed, already-reported settled
    spend out of a closed period. A forward re-anchor is exactly expressible
    as a close; a backward one is not expressible as anything honest.
    Re-anchoring returns in TBD-235 as an explicit, confirmed action.

    ``billing_service.get_current_period`` is still called: the audit payload
    names the period this change will NOT touch. Stated honestly, that call
    auto-creates and **commits** when the org has no open row (the
    ``if period is None`` branch of ``billing_service.get_current_period``),
    committing the pending ``billing_cycle_day`` with it. Benign — the new
    row lands on the new grid — and pre-existing.

    Pre-existing and NOT fixed here: if that auto-create loses an insert
    race, its ``IntegrityError`` branch calls ``db.rollback()``, which also
    discards the pending ``org.billing_cycle_day`` assignment above. The
    ``db.commit()`` below then writes nothing and this endpoint still
    returns 200 with a ``success`` audit row for a save that did not land.
    Narrow (needs two concurrent first-ever period creations for the same
    org) and out of scope for TBD-239.
    """
    _require_admin(current_user)

    # Snapshot actor identity before any await on db. No handler path rolls
    # back today, but `record_audit_event` opens its own session after the
    # request session has committed, and an expired `current_user` there
    # would break the audit row.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    result = await db.execute(
        select(Organization).where(Organization.id == actor_org_id)
    )
    org = result.scalar_one()
    org_name = org.name
    old_day = org.billing_cycle_day
    new_day = body.billing_cycle_day
    org.billing_cycle_day = new_day

    # The open period is NOT modified. It is read so the audit row can name
    # the period the new cycle day does not apply to.
    current_period = await billing_service.get_current_period(db, actor_org_id)
    period_id = current_period.id
    open_period_start = current_period.start_date

    async def _audit(outcome: str, **extra) -> None:
        """Structlog breadcrumb + audit row for this endpoint.

        ``record_audit_event`` swallows every exception by design
        (audit_service.py) and names "the structlog event the caller already
        emitted" as its fallback record, so the log line has to be emitted
        first — otherwise a transient audit-session failure leaves no trace
        at all that an org's billing cycle day changed.

        Written as a closure, and kept as one after TBD-239 removed the
        ``outcome="failure"`` call sites: the payload keys stay in one place
        for the failure branch TBD-235 will add back when re-anchoring
        returns as an explicit action.
        """
        payload = {
            "old_day": old_day,
            "new_day": new_day,
            "period_id": period_id,
            "open_period_start": open_period_start.isoformat(),
            "applies_from": "next_period",
            **extra,
        }
        await logger.ainfo(
            "org.billing_cycle_day.updated",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            outcome=outcome,
            **payload,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type="org.billing_cycle_day.updated",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=org_name,
            request_id=req_id,
            ip_address=ip,
            outcome=outcome,
            detail=payload,
        )

    await db.commit()

    await _audit("success")

    return {"billing_cycle_day": new_day}


@router.get("/billing-period")
async def get_current_period(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    period = await billing_service.get_current_period(db, current_user.org_id)
    return {
        "id": period.id,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat() if period.end_date else None,
    }


@router.get("/billing-periods")
async def list_periods(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    periods = await billing_service.list_periods(db, current_user.org_id)
    return [
        {
            "id": p.id,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat() if p.end_date else None,
        }
        for p in periods
    ]


# ═══════════════════════════════════════════════════════════════════════════
# TBD-234b — the read-only billing period roster.
#
# Spec: `specs/2026-07-29-billing-period-roster-design.md` §2.5, D1/D6-D10.
# The kernel it consumes (TBD-234a, `15faa922`) is FROZEN: this route fetches
# the roster, aggregates the money, windows the DISPLAY and renders §2.5's
# body. It changes nothing on the kernel side and writes nothing anywhere.
# ═══════════════════════════════════════════════════════════════════════════

#: D6. The display slice keeps the NEWEST rows past this count. The naive
#: alternative — the FIRST 200 — keeps the oldest rows and discards the open
#: row, every stub and every recent boundary.
ROSTER_DISPLAY_CAP = 200

#: D6/D8. `months` is clamped rather than rejected: out-of-range INTEGERS are
#: clamped, a non-integer still 422s in FastAPI's coercion layer before any
#: handler code runs, and this spec does not fight that.
ROSTER_MIN_MONTHS = 1
ROSTER_MAX_MONTHS = 60


def _roster_length_days(
    row: billing_service.RosterRow,
    effective_end: datetime.date | None,
    status: str,
) -> int | None:
    """Inclusive span, `null` where it would be meaningless.

    `null` when `effective_end` is null (the roster tail is genuinely
    unbounded) OR when the row is `invalid`, where
    `effective_end - start_date + 1` is negative and the status already
    carries the signal.
    """
    if effective_end is None or status == "invalid":
        return None
    return (effective_end - row.start_date).days + 1


async def _roster_transaction_count(
    db: AsyncSession,
    org_id: int,
    start: datetime.date,
    counting_through: datetime.date | None,
) -> int:
    """D7's UNFILTERED count, as a two-branch ``UNION ALL``.

    ⚠ **Two columns, two filters, two DIFFERENT predicate shapes**, and this
    is the un-filtered one. `list_transactions` applies no
    `reportable_transaction_filter`, so a filtered count would not match the
    click-through the page links to.

    With no status predicate, `ix_transactions_org_settled_date` is
    unreachable (its `status` column sits in the MIDDLE of the two useful
    ones) and a top-level `OR` across two columns degrades to an `org_id`
    prefix scan. The two branches are each sargable: one on
    `(org_id, ..., settled_date)`, the other on `ix_transactions_org_date`.
    Because `_apply_transaction_filters` runs `date_from`/`date_to` through
    `effective_period_date_expr()` — `coalesce(settled_date, date)` — this
    union is not an approximation of the click-through set, it **is** that
    set, decomposed into its two sargable halves.

    ⚠ **A null `counting_through` makes BOTH branches one-sided**, never one
    and not the other. That is the roster tail, where `effective_end` and
    `counting_through` are both null; the one-sided form keeps the index
    range intact and simply drops the trailing bound.
    """
    settled_branch = select(Transaction.id).where(
        Transaction.org_id == org_id,
        Transaction.settled_date >= start,
    )
    pending_branch = select(Transaction.id).where(
        Transaction.org_id == org_id,
        Transaction.settled_date.is_(None),
        Transaction.date >= start,
    )
    if counting_through is not None:
        settled_branch = settled_branch.where(Transaction.settled_date <= counting_through)
        pending_branch = pending_branch.where(Transaction.date <= counting_through)

    combined = union_all(settled_branch, pending_branch).subquery()
    return (
        await db.execute(select(func.count()).select_from(combined))
    ).scalar_one()


async def _roster_settled_net(
    db: AsyncSession,
    org_id: int,
    start: datetime.date,
    counting_through: datetime.date | None,
) -> Decimal:
    """D7's reportable settled net: income minus expense, in cents.

    The filtered column, and the opposite index story from the count above:
    pinning `status = SETTLED` makes this a clean three-column range on
    `ix_transactions_org_settled_date` = `(org_id, status, settled_date)`.
    **No `OR`, no `coalesce`** — the `settled_date IS NULL` disjunct is dead
    code under a `SETTLED` predicate (migration
    `036_settled_implies_settled_date` carries a real CHECK,
    `status <> 'settled' OR settled_date IS NOT NULL`, mirrored at flush
    time), and adding it would remove the range from the trailing key part
    and collapse the plan to `(org_id, status)`.

    One-sided when `counting_through` is null, exactly as the count above.
    """
    signed = case(
        (Transaction.type == TransactionType.INCOME, Transaction.amount),
        else_=-Transaction.amount,
    )
    stmt = select(func.coalesce(func.sum(signed), 0)).where(
        Transaction.org_id == org_id,
        reportable_transaction_filter(),
        Transaction.status == TransactionStatus.SETTLED,
        Transaction.settled_date >= start,
    )
    if counting_through is not None:
        stmt = stmt.where(Transaction.settled_date <= counting_through)
    return Decimal(str((await db.execute(stmt)).scalar_one()))


@router.get("/billing-periods/roster", response_model=RosterResponse)
async def get_billing_period_roster(
    months: int = 12,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Everything wrong with this org's billing period roster, plus the money.

    **Read-only, and that is load-bearing.** Five shipped tickets deferred
    residual corruption to a detector that did not exist, and today the
    fleet's roster health is observable only by grepping structlog.

    Three rules this handler exists to obey:

    * **D10 — never call ``get_current_period``.** It auto-creates *and
      commits* a `BillingPeriod` when none is open, and "no open row" is one
      of the anomalies this page exists to *report*. A read-only diagnostic
      that manufactures the row it reports on is disqualifying.
    * **D6 — ONE fetch of ``billing_periods``.** The display window is a
      Python slice over :func:`load_complete_roster`'s result, never a second
      ``SELECT``. A second windowed query buys nothing and costs correctness:
      an insert landing between the two makes `roster.period_count` and
      `periods` describe different rosters, which is the scope confusion
      §2.5's two-scope contract exists to prevent, reintroduced at the query
      layer. Fenced by test 32's statement counter.
    * **D8a — resolve the clock ONCE.** ``today`` goes to `period_status`, to
      `find_period_anomalies` and to `period_spend_window_end`. Without it a
      request straddling UTC midnight can classify a row `past` against day
      D while computing its window against day D+1, producing a
      self-contradictory row on the page whose entire job is catching
      self-contradictory rows.

    **Two scopes, never conflated.** `roster` is org-wide and is the anomaly
    domain; `window` is display only. Analysis quantifies over the COMPLETE
    roster, so a truncated page still reports every marker, with the
    off-window ones resolvable through `referenced_periods`. That is what
    lets the page make the strong claim — *absence of markers means the
    roster is healthy* — rather than the weak one a windowed design could
    only make.

    Query budget, accepted rather than hidden: 1 roster fetch + 2 aggregates
    per displayed row + ~1 per displayed OPEN row for `counting_through`
    (`period_effective_end` costs ZERO queries on a closed row), so ~402 at
    the 200-row cap. DO App Platform's request timeout binds before the cap
    does; the fix is the recorded single-JOIN alternative (spec D6), not a
    raised cap.
    """
    _require_admin(current_user)
    org_id = current_user.org_id

    # D8a — once, here, and passed to every callee.
    today = datetime.date.today()

    roster = await billing_service.load_complete_roster(db, org_id)
    anomalies = billing_service.find_period_anomalies(roster, today=today)

    # Derived ends for EVERY row, not only the displayed ones:
    # `referenced_periods` must carry `effective_end` for off-window ids too.
    effective_ends = [
        billing_service.kernel_derived_end(roster, i) for i in range(len(roster.rows))
    ]
    statuses = [billing_service.period_status(row, today=today) for row in roster.rows]

    # ── the display window: a Python slice, never a second SELECT ────────
    months = min(max(months, ROSTER_MIN_MONTHS), ROSTER_MAX_MONTHS)
    cutoff = today - relativedelta(months=months)
    in_window = [
        i for i, row in enumerate(roster.rows) if row.start_date >= cutoff
    ]
    truncated = len(in_window) > ROSTER_DISPLAY_CAP
    # `roster.rows` is already `start_date` ASC, so the newest rows are the
    # LAST ones and the slice needs no re-sorting at all.
    displayed = in_window[-ROSTER_DISPLAY_CAP:] if truncated else in_window

    periods: list[RosterPeriod] = []
    for i in displayed:
        row = roster.rows[i]
        # B4: the roster row is passed straight through. Re-materialising an
        # ORM entity here would trip D6's single-fetch rule.
        counting_through = await billing_service.period_spend_window_end(
            db, org_id, row, today=today
        )
        periods.append(
            RosterPeriod(
                id=row.id,
                start_date=row.start_date,
                end_date=row.end_date,
                effective_end=effective_ends[i],
                counting_through=counting_through,
                status=statuses[i],
                length_days=_roster_length_days(row, effective_ends[i], statuses[i]),
                transaction_count=await _roster_transaction_count(
                    db, org_id, row.start_date, counting_through
                ),
                settled_net=str(
                    (
                        await _roster_settled_net(
                            db, org_id, row.start_date, counting_through
                        )
                    ).quantize(Decimal("0.01"))
                ),
            )
        )

    displayed_ids = {roster.rows[i].id for i in displayed}
    by_id = {row.id: i for i, row in enumerate(roster.rows)}

    wire_anomalies = []
    referenced: dict[str, ReferencedPeriod] = {}
    for anomaly in anomalies:
        ids = anomaly_referenced_ids(anomaly)
        wire_anomalies.append(
            to_wire_anomaly(
                anomaly,
                off_window=any(pid not in displayed_ids for pid in ids),
            )
        )
        for pid in ids:
            if str(pid) in referenced:
                continue
            # Direct indexing, never `.get`: every id a marker names came out
            # of the same `CompleteRoster`, so a miss is a kernel bug and
            # must surface rather than silently drop the entry the page needs
            # to render an off-window marker.
            i = by_id[pid]
            row = roster.rows[i]
            referenced[str(pid)] = ReferencedPeriod(
                id=row.id,
                start_date=row.start_date,
                end_date=row.end_date,
                effective_end=effective_ends[i],
                status=statuses[i],
            )

    return RosterResponse(
        roster=RosterScope(
            # ⚠ Served from the `CompleteRoster` the kernel received, never
            # from an independent `SELECT COUNT(*)`: without that identity,
            # test 22 goes green on exactly the wiring it exists to forbid.
            period_count=len(roster.rows),
            first_start=roster.rows[0].start_date if roster.rows else None,
            last_start=roster.rows[-1].start_date if roster.rows else None,
            # A scope-level restatement of a marker the kernel already emits,
            # not a second source of truth. ⚠ It names an OVERLAP-only
            # refusal: the other eight rules always ran.
            analyzed=not any(
                a.kind == "overlap_analysis_skipped" for a in anomalies
            ),
        ),
        window=WindowScope(
            from_=periods[0].start_date if periods else None,
            to=None,
            displayed_count=len(periods),
            truncated=truncated,
        ),
        periods=periods,
        anomalies=wire_anomalies,
        referenced_periods=referenced,
    )


@router.post("/billing-period", status_code=200)
async def create_period(
    body: BillingPeriodCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a billing period with explicit dates (for seeding/migration).

    ``status_code`` stays 200 — the pre-existing contract of an endpoint whose
    only callers live in ``seed.py``; there is nothing to gain by churning it
    to 201. ``seed.py`` no longer branches on the status code itself: it hands
    the response to ``seed.billing_period_outcome``, which absorbs the 409
    below (the seed dataset is re-runnable and its start dates are
    deterministic) and raises on everything else.
    """
    _require_admin(current_user)

    existing = await db.scalar(
        select(BillingPeriod.id).where(
            BillingPeriod.org_id == current_user.org_id,
            BillingPeriod.start_date == body.start_date,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"A billing period already starts on {body.start_date.isoformat()}",
            code="billing_period_exists",
        )

    # Containment (TBD-239 §3). The exact-start check above is deliberately
    # first: `./pfv seed` re-runs post start dates that already exist and
    # `seed.billing_period_outcome` reads the code, so those rows must keep
    # answering `billing_period_exists`.
    #
    # NULL semantics, pinned in both directions:
    #
    # * A CLOSED existing row (arm 1) is compared on its RAW `end_date`,
    #   window against window.
    # * An OPEN existing row (arm 2) has an unknowable end, so it cannot be
    #   intersected as a window. Its `start_date` is perfectly knowable
    #   though, and the candidate's window is fully known, so an open row
    #   whose start falls inside [candidate.start, candidate_end] is a
    #   PROVABLE overlap and is rejected. Only the unprovable part (whether
    #   the open row extends past the candidate) is waved through. An
    #   earlier revision skipped open rows entirely and let that provable
    #   case land: repeated `./pfv seed` runs produced closed rows that
    #   swallowed an open row's start.
    # * The CANDIDATE is checked on its `start_date` alone when it carries no
    #   `end_date` (`BillingPeriodCreate.end_date` is optional and
    #   `seed.py:260-261` posts exactly that shape). Treating an open
    #   candidate as unbounded would make seeding an open period after any
    #   closed period conflict every time.
    candidate_end = body.end_date or body.start_date
    overlap = (
        await db.execute(
            select(BillingPeriod)
            .where(
                BillingPeriod.org_id == current_user.org_id,
                BillingPeriod.start_date <= candidate_end,
                or_(
                    and_(
                        BillingPeriod.end_date.is_not(None),
                        BillingPeriod.end_date >= body.start_date,
                    ),
                    and_(
                        BillingPeriod.end_date.is_(None),
                        BillingPeriod.start_date >= body.start_date,
                    ),
                ),
            )
            .order_by(BillingPeriod.start_date)
            .limit(1)
        )
    ).scalars().first()
    if overlap is not None:
        # SELECT-then-INSERT, therefore TOCTOU under real MySQL, and unlike
        # the exact-start case no unique constraint backstops an intersecting
        # row. Best-effort by design; detection of what slips through is
        # TBD-234's anomaly kernel.
        covers = (
            f"starts on {overlap.start_date.isoformat()} and is still open"
            if overlap.end_date is None
            else (
                f"covers {overlap.start_date.isoformat()} to "
                f"{overlap.end_date.isoformat()}"
            )
        )
        raise ConflictError(
            f"A billing period already {covers}. "
            "Choose dates outside that range.",
            code="billing_period_overlap",
        )

    period = BillingPeriod(
        org_id=current_user.org_id,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    db.add(period)
    try:
        await db.commit()
    except IntegrityError:
        # TOCTOU backstop for uq_billing_period_org_start.
        await db.rollback()
        raise ConflictError(
            f"A billing period already starts on {body.start_date.isoformat()}",
            code="billing_period_exists",
        )
    await db.refresh(period)
    return {
        "id": period.id,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat() if period.end_date else None,
    }


@router.post("/billing-periods/ensure-future")
async def ensure_future_periods(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    count: int = 3,
):
    """Create stub periods for upcoming months so the user can plan ahead."""
    _require_admin(current_user)
    count = min(max(count, 1), 6)  # Cap between 1 and 6 months
    created = await billing_service.ensure_future_periods(db, current_user.org_id, count=count)
    return [
        {
            "id": p.id,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat() if p.end_date else None,
        }
        for p in created
    ]


@router.post("/billing-period/close")
async def close_period(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    close_date: datetime.date | None = None,
):
    """Close the open period and open the next one.

    ``close_period`` commits internally and returns only the NEW period, so
    the audit detail is assembled around it: the closing period is snapshotted
    via ``get_current_period`` BEFORE the call, and the resolved close date is
    derived as ``new_period.start_date - 1 day``. Re-implementing the service's
    "yesterday" default here would drift from it.

    Since TBD-241 the service also CLAMPS the close to the first intervening
    period boundary, so the resolved date is not always the requested one. The
    derivation above needs no change for that — it reads the row the service
    actually opened — which is why the audit key ``close_date`` reports the
    clamped date for free. One click closes one period; convergence for a
    lapsed org is ``BillingCloseJob``'s job, not this route's.
    """
    _require_admin(current_user)

    # Snapshot actor identity before any await on db so a rollback path
    # can't expire `current_user` and break the audit row.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    org_name = await db.scalar(
        select(Organization.name).where(Organization.id == actor_org_id)
    )

    closing = await billing_service.get_current_period(db, actor_org_id)
    closed_period_id = closing.id
    closed_period_start = closing.start_date

    async def _audit(outcome: str, **extra) -> None:
        """Structlog breadcrumb + audit row for this endpoint.

        ``record_audit_event`` swallows every exception by design
        (audit_service.py) and names "the structlog event the caller already
        emitted" as its fallback record, so the log line goes first.
        """
        payload = {
            "closed_period_id": closed_period_id,
            "closed_period_start": closed_period_start.isoformat(),
            **extra,
        }
        await logger.ainfo(
            "org.billing_period.closed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            outcome=outcome,
            **payload,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type="org.billing_period.closed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=org_name,
            request_id=req_id,
            ip_address=ip,
            outcome=outcome,
            detail=payload,
        )

    try:
        new_period = await billing_service.close_period(db, actor_org_id, close_date)
    except Exception as exc:  # noqa: BLE001 — nothing may close unaudited.
        # ValidationError (close date before the period start) is not the only
        # way out of `close_period`: it also raises RuntimeError when the row
        # vanishes after its own IntegrityError retry, and IntegrityError can
        # escape its second commit. Catching only ValidationError left both as
        # unaudited 500s. `org_data.py`'s reset path is the house reference:
        # catch broadly, audit, re-raise untouched.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 — best effort; the audit matters more
            pass
        await _audit(
            "failure",
            close_date=close_date.isoformat() if close_date else None,
            reason="validation" if isinstance(exc, ValidationError) else "error",
            message=getattr(exc, "detail", None) or str(exc),
            error_type=type(exc).__name__,
        )
        raise

    resolved_close_date = new_period.start_date - datetime.timedelta(days=1)

    await _audit(
        "success",
        close_date=resolved_close_date.isoformat(),
        # TBD-241 D10: a VERBATIM echo of the raw parameter, null when absent.
        # Never re-derive the service's "yesterday" default to fill it — that is
        # what the docstring above forbids, and under D2 it would additionally
        # drift, because this route deliberately does not pass `today`.
        # Honest limitation: the UI sends no `close_date`, so this key is null
        # for every human close and the audit row alone cannot distinguish
        # "asked for 07-27, clamped to 05-24" from "asked for 05-24". The clamp
        # signal is the service's `billing.close.clamped` event; this key earns
        # its place for API and PAT callers.
        requested_close_date=close_date.isoformat() if close_date else None,
        new_period_id=new_period.id,
        new_period_start=new_period.start_date.isoformat(),
    )

    return {
        "id": new_period.id,
        "start_date": new_period.start_date.isoformat(),
        "end_date": None,
    }


# ── Track E: manual balance adjustment toggle ─────────────────────────────


@router.get(
    "/manual-balance-adjustment",
    response_model=ManualBalanceAdjustmentResponse,
)
async def get_manual_balance_adjustment(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current org's manual-balance-adjustment toggle.
    Available to any org member (the frontend uses it to render or hide
    the "Adjust balance" button on each account card).
    """
    org = await db.scalar(
        select(Organization).where(Organization.id == current_user.org_id)
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return ManualBalanceAdjustmentResponse(
        enabled=org.allow_manual_balance_adjustment
    )


@router.put(
    "/manual-balance-adjustment",
    response_model=ManualBalanceAdjustmentResponse,
)
async def update_manual_balance_adjustment(
    body: ManualBalanceAdjustmentToggle,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Track E: admin-only toggle for manual balance adjustment.

    Writes an audit row even on no-op (old == new) so a paranoid admin
    can confirm "yes, I checked the toggle and it's still off". The
    audit row commits in an independent session via ``record_audit_event``
    AFTER the business commit so the admin's UI doesn't hang on audit
    DB hiccups, and an audit failure can never roll back a successful
    toggle write.
    """
    _require_admin(current_user)

    # Snapshot actor identity before any await on db so a rollback path
    # can't expire `current_user` and break the audit row.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    org = await db.scalar(
        select(Organization).where(Organization.id == actor_org_id)
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    old_value = bool(org.allow_manual_balance_adjustment)
    new_value = bool(body.enabled)
    org.allow_manual_balance_adjustment = new_value
    org_name = org.name
    await db.commit()

    await logger.ainfo(
        "org.config.allow_manual_balance_adjustment.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=actor_org_id,
        old=old_value,
        new=new_value,
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="org.config.allow_manual_balance_adjustment.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=actor_org_id,
        target_org_name=org_name,
        request_id=req_id,
        ip_address=ip,
        outcome="success",
        detail={"old": old_value, "new": new_value},
    )

    return ManualBalanceAdjustmentResponse(enabled=new_value)
