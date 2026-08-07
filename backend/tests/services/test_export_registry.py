"""Completeness fences for the org data export registry (TBD-222, spec §4).

``wipe_org_data`` covers 15 of 49 tables under a docstring promising *"every
new org-scoped data table goes through this function"*. **A convention in a
docstring is not a mechanism.** These are the mechanism.

Four legs, each closing a blind spot in the one before it:

* **Leg 1** — runtime SQLAlchemy metadata vs the hand-written registry, in
  BOTH directions.
* **Leg 2** — the FILESYSTEM vs runtime metadata. ⚠ ``Base.metadata`` is
  populated only by the imports in ``app/models/__init__.py``, so a model
  file that is never imported is invisible to leg 1 *and* to alembic
  autogenerate. Leg 1 stays GREEN for that bug; only leg 2 sees it.
* **Leg 3** — the registry must be the thing the exporter LOOPS over. A
  registry that decides but is not iterated is decoration, and "correct
  registry, hand-maintained exporter" is this repo's signature half-fix.
* **Leg 4** — the scoping predicate. ⚠ ``org_id`` is not a sufficient
  predicate: six included tables reach the org only by join and two have a
  nullable ``org_id``.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models
from app.models.base import Base
from app.services import org_export_service
from app.services.export_registry import (
    EXPORT_DISPOSITION,
    SECRET_NAME_ALLOWLIST,
    SECRET_NAME_PATTERNS,
    Include,
    excluded_reasons,
    included_tables,
)
from app.services.org_export_service import collect_export, stream_org_export


# ══ Leg 1 — runtime metadata vs the hand-written literal ═══════════════════
#
# Not tautological. ``runtime`` is produced by class creation, authored by
# whoever adds a model — who is by hypothesis not thinking about the export.
# ``declared`` is typed by a human who is.


def test_leg1_every_runtime_table_has_a_disposition():
    """A new model with no export decision fails CI."""
    runtime = set(Base.metadata.tables)
    declared = set(EXPORT_DISPOSITION)
    missing = runtime - declared
    assert not missing, (
        f"{len(missing)} table(s) exist at runtime with no export disposition: "
        f"{sorted(missing)}. Add an Include(...) or Exclude(...) entry to "
        f"app/services/export_registry.py."
    )


def test_leg1_no_stale_disposition_for_a_dropped_table():
    """⚠ The reverse direction. Without it a renamed table leaves a dead
    entry that makes the export's own counts lie forever."""
    runtime = set(Base.metadata.tables)
    declared = set(EXPORT_DISPOSITION)
    stale = declared - runtime
    assert not stale, (
        f"{len(stale)} disposition(s) name tables that no longer exist: "
        f"{sorted(stale)}."
    )


# ══ Leg 2 — filesystem vs runtime metadata ════════════════════════════════


def _tablenames_in_source() -> set[str]:
    """Scrape ``__tablename__`` literals straight off disk.

    Deliberately does NOT import anything. That is the entire point: the bug
    this leg catches is a model file that is never imported.
    """
    models_dir = Path(app.models.__file__).parent
    found: set[str] = set()
    for path in sorted(models_dir.glob("*.py")):
        for match in re.finditer(
            r'^\s*__tablename__\s*=\s*["\'](\w+)["\']',
            path.read_text(),
            re.M,
        ):
            found.add(match.group(1))
    return found


def test_leg2_source_tablenames_match_runtime_metadata():
    """⚠ Closes leg 1's blind spot.

    ``Base.metadata`` is populated ONLY by the imports in
    ``app/models/__init__.py``. A model file not listed there is invisible to
    ``Base.metadata`` **and** to ``alembic/env.py``'s ``target_metadata`` — so
    the table gets no migration, and leg 1 stays green while it exists.
    """
    in_source = _tablenames_in_source()
    in_runtime = set(Base.metadata.tables)

    unimported = in_source - in_runtime
    assert not unimported, (
        f"{len(unimported)} model(s) define __tablename__ but are never "
        f"imported by app/models/__init__.py: {sorted(unimported)}. They are "
        f"invisible to Base.metadata AND to alembic autogenerate."
    )
    assert not (in_runtime - in_source), (
        f"tables in metadata with no __tablename__ literal on disk: "
        f"{sorted(in_runtime - in_source)}"
    )


def test_table_count_is_what_the_spec_measured():
    """Anchors the count the spec was written against.

    ⚠ This number drifted three times across this sprint's own briefs about
    drift (44 → 49 → "37 files" → 35). Not a correctness gate — legs 1 and 2
    are — but a loud signal when the surface moves.
    """
    assert len(Base.metadata.tables) == 49
    assert len(EXPORT_DISPOSITION) == 49


# ══ Fixtures for the data-path legs ═══════════════════════════════════════


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_org(db: AsyncSession, *, marker: str) -> dict:
    """Seed one row in EVERY included table, tagged with ``marker``.

    ⚠ Leg 4 cannot see a cross-tenant leak unless BOTH orgs have rows in
    every included table. An org B that is missing rows in the six join-only
    tables would make an unfiltered read on those tables look clean.

    Returns ``{"org_id", "user_id", "account_id", "transaction_id", ...}``.
    """
    from app.models.account import Account, AccountType
    from app.models.ai_usage_ledger import AIUsageLedger
    from app.models.announcement import (
        Announcement,
        AnnouncementSeverity,
        UserDismissedAnnouncement,
    )
    from app.models.audit_event import AuditEvent, AuditOutcome
    from app.models.billing import BillingPeriod
    from app.models.budget import Budget
    from app.models.category import Category, CategoryType
    from app.models.category_rule import CategoryRule, RuleSource
    from app.models.cc_cycle_payment import CcCyclePayment
    from app.models.dashboard import DashboardLayout
    from app.models.email_broadcast import (
        BroadcastStatus,
        EmailBroadcast,
        EmailBroadcastRecipient,
        RecipientStatus,
    )
    from app.models.feedback import FeedbackCategory, FeedbackEntry
    from app.models.forecast_plan import (
        ForecastItemType,
        ForecastPlan,
        ForecastPlanItem,
        ItemSource,
        PlanStatus,
    )
    from app.models.import_batch import (
        ImportBatch,
        ImportBatchStatus,
        ImportSourceFormat,
    )
    from app.models.invitation import Invitation
    from app.models.notification import (
        Notification,
        NotificationCategory,
        UserNotificationPreferences,
    )
    from app.models.org_ai_caps import OrgAIDefaultCaps, OrgAIFeatureCaps
    from app.models.org_ai_consent import OrgAIConsent
    from app.models.org_ai_credential import AiProvider, OrgAICredential
    from app.models.org_ai_routing import OrgAIDefaultRouting, OrgAIFeatureRouting
    from app.models.recurring import Frequency, RecurringTransaction
    from app.models.report import Report, ReportVersion, ReportVisibility
    from app.models.scenario import Scenario, ScenarioType
    from app.models.settings import OrgSetting
    from app.models.subscription import (
        BillingInterval,
        Plan,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.tag import Tag, TransactionTag
    from app.models.transaction import Transaction, TransactionStatus, TransactionType
    from app.models.user import Organization, Role, User

    import datetime

    today = datetime.date(2026, 8, 7)
    now = datetime.datetime(2026, 8, 7, 12, 0, 0)

    org = Organization(name=f"Org-{marker}", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    oid = org.id

    user = User(
        org_id=oid,
        username=f"user-{marker}",
        email=f"{marker}@example.test",
        password_hash=f"HASH-{marker}",
        totp_secret=f"TOTP-{marker}",
        recovery_codes=f"RECOVERY-{marker}",
        stepup_token=f"STEPUP-{marker}",
        stepup_token_expires_at=now,
        password_changed_at=now,
        sessions_invalidated_at=now,
        role=Role.OWNER,
        is_superadmin=False,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    await db.flush()
    uid = user.id

    plan = (
        await db.execute(select(Plan).where(Plan.slug == "free"))
    ).scalar_one_or_none()
    if plan is None:
        plan = Plan(slug="free", name="Free")
        db.add(plan)
        await db.flush()

    db.add(
        Subscription(
            org_id=oid,
            plan_id=plan.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_start=today,
            trial_end=today + datetime.timedelta(days=14),
        )
    )
    db.add(OrgSetting(org_id=oid, key="theme", value=f"SETTING-{marker}"))

    atype = AccountType(org_id=oid, name=f"Checking-{marker}", slug=f"chk-{marker}")
    db.add(atype)
    await db.flush()
    account = Account(
        org_id=oid,
        account_type_id=atype.id,
        name=f"ACCOUNT-{marker}",
        balance=Decimal("100.00"),
    )
    db.add(account)
    await db.flush()
    aid = account.id

    # Join-only via accounts.
    db.add(
        CcCyclePayment(
            account_id=aid,
            period_anchor_year=2026,
            period_anchor_month=1,
            amount=Decimal("42.00"),
        )
    )

    category = Category(org_id=oid, name=f"CATEGORY-{marker}", type=CategoryType.EXPENSE)
    db.add(category)
    await db.flush()
    cid = category.id

    db.add(
        CategoryRule(
            org_id=oid,
            category_id=cid,
            normalized_token=f"rule-{marker}",
            raw_description_seen=f"RULE-{marker}",
            source=RuleSource.USER_EDIT,
        )
    )

    txn = Transaction(
        org_id=oid,
        account_id=aid,
        category_id=cid,
        amount=Decimal("10.00"),
        description=f"TRANSACTION-{marker}",
        date=today,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        settled_date=today,
    )
    db.add(txn)
    await db.flush()
    tid = txn.id

    tag = Tag(org_id=oid, name=f"TAG-{marker}", name_normalized=f"tag-{marker}")
    db.add(tag)
    await db.flush()
    # Join-only via transactions.
    db.add(TransactionTag(transaction_id=tid, tag_id=tag.id))

    db.add(
        Budget(
            org_id=oid,
            category_id=cid,
            amount=Decimal("500.00"),
            period_start=today,
        )
    )
    period = BillingPeriod(
        org_id=oid,
        start_date=today,
        end_date=today + datetime.timedelta(days=30),
    )
    db.add(period)
    await db.flush()
    db.add(
        RecurringTransaction(
            org_id=oid,
            account_id=aid,
            category_id=cid,
            amount=Decimal("9.99"),
            description=f"RECURRING-{marker}",
            type=TransactionType.EXPENSE,
            frequency=Frequency.MONTHLY,
            next_due_date=today,
        )
    )

    fplan = ForecastPlan(
        org_id=oid, billing_period_id=period.id, status=PlanStatus.DRAFT
    )
    db.add(fplan)
    await db.flush()
    db.add(
        ForecastPlanItem(
            org_id=oid,
            plan_id=fplan.id,
            category_id=cid,
            type=ForecastItemType.EXPENSE,
            source=ItemSource.MANUAL,
            planned_amount=Decimal("5.00"),
        )
    )

    db.add(
        ImportBatch(
            org_id=oid,
            account_id=aid,
            file_name=f"IMPORT-{marker}.csv",
            source_format=ImportSourceFormat.CSV,
            status=ImportBatchStatus.CLOSED,
            created_by_user_id=uid,
        )
    )
    db.add(
        Invitation(
            org_id=oid,
            email=f"INVITE-{marker}@example.test",
            role=Role.MEMBER,
            created_by=uid,
            expires_at=now + datetime.timedelta(days=7),
        )
    )
    db.add(
        FeedbackEntry(
            org_id=oid,
            user_id=uid,
            category=FeedbackCategory.BUG,
            message=f"FEEDBACK-{marker}",
            context={"page": f"CONTEXT-{marker}"},
        )
    )

    report = Report(
        org_id=oid,
        owner_user_id=uid,
        name=f"REPORT-{marker}",
        visibility=ReportVisibility.PRIVATE,
        layout_json=[],
        canvas_filters_json={},
    )
    db.add(report)
    await db.flush()
    # Join-only via reports.
    db.add(
        ReportVersion(
            report_id=report.id, is_original=True, layout_json=[], canvas_filters_json={}
        )
    )

    db.add(
        DashboardLayout(
            org_id=oid,
            owner_user_id=uid,
            layout_json=[f"DASHBOARD-{marker}"],
            canvas_filters_json={},
        )
    )
    db.add(
        Scenario(
            org_id=oid,
            user_id=uid,
            name=f"SCENARIO-{marker}",
            scenario_type=ScenarioType.CUSTOM,
            params_json={"m": f"SCENARIOPARAM-{marker}"},
            horizon_months=12,
        )
    )

    # AI subsystem.
    cred = OrgAICredential(
        org_id=oid,
        provider=AiProvider.OPENAI,
        encrypted_api_key=f"ENCKEY-{marker}",
        encrypted_bearer_token=f"ENCBEARER-{marker}",
        key_fingerprint=f"FINGERPRINT-{marker}",
        base_url=f"https://user:BASEURL-{marker}@ai.example.test",
        label=f"CRED-{marker}",
    )
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=oid, credential_id=cred.id, model=f"MODEL-{marker}"))
    db.add(
        OrgAIFeatureRouting(
            org_id=oid,
            feature_name="forecast_refine",
            credential_id=cred.id,
            model=f"FMODEL-{marker}",
        )
    )
    db.add(OrgAIDefaultCaps(org_id=oid, soft_cap_cents=100, hard_cap_cents=200))
    db.add(
        OrgAIFeatureCaps(
            org_id=oid,
            feature_key="forecast_refine",
            soft_cap_cents=10,
            hard_cap_cents=20,
        )
    )
    db.add(
        OrgAIConsent(
            org_id=oid,
            consent_version=f"CONSENT-{marker}",
            consented_by_user_id=uid,
        )
    )
    db.add(
        AIUsageLedger(
            org_id=oid,
            feature_key="forecast_refine",
            model=f"LEDGER-{marker}",
            credential_id=cred.id,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )
    )

    # audit_events — scoped by target_org_id ONLY.
    db.add(
        AuditEvent(
            event_type="org.renamed",
            actor_user_id=uid,
            actor_email=f"AUDITACTOR-{marker}@example.test",
            target_org_id=oid,
            target_org_name=f"Org-{marker}",
            ip_address=f"IP-{marker}",
            outcome=AuditOutcome.SUCCESS,
            detail={"leak": f"DETAIL-{marker}"},
        )
    )

    # Join-only via users.
    db.add(
        Notification(
            user_id=uid,
            category=NotificationCategory.SECURITY,
            event_type="x",
            title=f"NOTIFICATION-{marker}",
            body="b",
        )
    )
    db.add(UserNotificationPreferences(user_id=uid))
    ann = Announcement(
        title=f"ANN-{marker}", body="b", severity=AnnouncementSeverity.INFO
    )
    db.add(ann)
    await db.flush()
    db.add(UserDismissedAnnouncement(user_id=uid, announcement_id=ann.id))

    broadcast = EmailBroadcast(
        subject=f"BROADCAST-{marker}",
        body_template="b",
        segment="all",
        status=BroadcastStatus.COMPLETED,
    )
    db.add(broadcast)
    await db.flush()
    db.add(
        EmailBroadcastRecipient(
            broadcast_id=broadcast.id,
            user_id=uid,
            email=f"RECIPIENT-{marker}@example.test",
            status=RecipientStatus.SENT,
            error=f"MAILGUNERROR-{marker}",
        )
    )

    await db.commit()
    return {"org_id": oid, "user_id": uid, "account_id": aid, "transaction_id": tid}


@pytest_asyncio.fixture
async def two_orgs(session_factory):
    """Org A (the export subject) and org B (the cross-tenant control).

    Both are seeded through the SAME helper, so B has a row in every table A
    does — including the six join-only ones.
    """
    async with session_factory() as db:
        a = await _seed_org(db, marker="AAA")
    async with session_factory() as db:
        b = await _seed_org(db, marker="BBB")
    return {"a": a, "b": b, "factory": session_factory}


# ══ Leg 3 — the registry must be the loop ═════════════════════════════════


@pytest.mark.asyncio
async def test_leg3_exporter_emits_exactly_the_included_tables(two_orgs):
    """A registry that decides but is not iterated is decoration."""
    async with two_orgs["factory"]() as db:
        result = await collect_export(db, org_id=two_orgs["a"]["org_id"])

    assert set(result.tables) == included_tables(), (
        "the exporter's table set diverged from the registry: "
        f"missing={sorted(included_tables() - set(result.tables))} "
        f"extra={sorted(set(result.tables) - included_tables())}"
    )


@pytest.mark.asyncio
async def test_leg3_every_included_table_emitted_exactly_one_row(two_orgs):
    """Guards the fixture AND carries leg 4's per-table count fence.

    ⚠ ``!= 1``, not ``== 0``. The weaker "no empty table" form guarded the
    fixture only, which left leg 4 blind for every table the sentinel regex
    could not see: a mutant reading ``users`` with ``.where(true())`` passed
    the whole file green while org B's usernames, emails, ``is_superadmin``,
    ``email_verified``, ``password_changed_at`` and
    ``sessions_invalidated_at`` shipped inside org A's artifact.

    ``_seed_org`` puts exactly one row per org in every included table
    (measured: no exceptions), so **2 means the org filter was dropped and 0
    means the fixture went vacuous** — this one assertion closes both, for
    all 36 tables, with nothing hand-maintained to rot. It replaces a
    hardcoded 7-table list that covered 26 of the 36.
    """
    async with two_orgs["factory"]() as db:
        result = await collect_export(db, org_id=two_orgs["a"]["org_id"])

    wrong = sorted((t, n) for t, n in result.tables.items() if n != 1)
    assert not wrong, (
        f"{len(wrong)} included table(s) did not emit exactly org A's one "
        f"row: {wrong}. 2+ means the org filter was dropped (a cross-tenant "
        f"leak); 0 means the seed helper forgot the table, which would make "
        f"the sentinel fence vacuous for it."
    )


# ══ Leg 4 — the scoping predicate ═════════════════════════════════════════


async def _export_text(factory, org_id: int) -> str:
    chunks: list[bytes] = []
    async with factory() as db:
        async for line in stream_org_export(db, org_id=org_id, org_name="A"):
            chunks.append(line)
    return b"".join(chunks).decode()


@pytest.mark.asyncio
async def test_leg4_no_org_b_sentinel_appears_in_org_a_export(two_orgs):
    """⚠ ``org_id`` is not a sufficient predicate.

    Six included tables reach the org only by join and two have a nullable
    ``org_id``, so a blanket ``WHERE org_id = ?`` — or the lazy fix of
    dropping the filter on a table that has no such column — leaks. Org B is
    seeded in every included table precisely so this can see it.

    ⚠ The pattern is deliberately NOT ``[A-Z]+-BBB``. That form needs an
    uppercase run immediately before the marker and so could not see
    ``Org-BBB``, ``Checking-BBB``, ``user-BBB`` or ``BBB@example.test`` —
    which is precisely the set a leak of ``organizations``, ``account_types``
    or ``users`` produces. Match ``BBB`` anywhere and report its context.
    """
    text = await _export_text(two_orgs["factory"], two_orgs["a"]["org_id"])

    assert "-AAA" in text, "sanity: org A's own rows must be present"
    leaked = sorted({m for m in re.findall(r"[A-Za-z0-9_.@:/-]*BBB[A-Za-z0-9_.@:/-]*", text)})
    assert not leaked, f"org B data leaked into org A's export: {leaked}"


@pytest.mark.asyncio
async def test_leg4_audit_events_scoped_by_target_org_only(two_orgs):
    """⚠ Never an ``actor_user_id`` disjunct.

    A superadmin's ``admin.*`` action against another tenant carries their
    ``actor_user_id`` with a FOREIGN ``target_org_id``. A disjunct would
    therefore export the other tenant's audit rows. This seeds exactly that
    row and asserts it stays out.

    ⚠ The sentinel lives in ``target_org_name``, an EXPORTED column. It used
    to live in ``detail`` — which ``audit_events`` REDACTS, so the assertion
    was unreachable by construction and passed whatever the scope did. A
    fence suppressed by a sibling rule is not a fence.
    """
    from app.models.audit_event import AuditEvent, AuditOutcome

    a_uid = two_orgs["a"]["user_id"]
    b_oid = two_orgs["b"]["org_id"]
    async with two_orgs["factory"]() as db:
        db.add(
            AuditEvent(
                event_type="admin.org.renamed",
                actor_user_id=a_uid,               # org A's superadmin ...
                actor_email="a-admin@example.test",
                target_org_id=b_oid,               # ... acting on org B
                target_org_name="CROSSTENANT-BBB",  # exported, not redacted
                outcome=AuditOutcome.SUCCESS,
                detail={"leak": "REDACTEDANYWAY-BBB"},
            )
        )
        await db.commit()

    text = await _export_text(two_orgs["factory"], two_orgs["a"]["org_id"])
    assert "CROSSTENANT-BBB" not in text
    assert "admin.org.renamed" not in text


# ══ §6 — redaction ════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_secrets_never_appear_in_the_export(two_orgs):
    """Every value the registry redacts, seeded as a unique sentinel."""
    text = await _export_text(two_orgs["factory"], two_orgs["a"]["org_id"])

    for sentinel in (
        "HASH-AAA",            # users.password_hash
        "TOTP-AAA",            # users.totp_secret
        "RECOVERY-AAA",        # users.recovery_codes
        "STEPUP-AAA",          # users.stepup_token
        "ENCKEY-AAA",          # org_ai_credentials.encrypted_api_key
        "ENCBEARER-AAA",       # org_ai_credentials.encrypted_bearer_token
        "FINGERPRINT-AAA",     # org_ai_credentials.key_fingerprint
        "BASEURL-AAA",         # ⚠ matches NO secret-name pattern
        "IP-AAA",              # audit_events.ip_address
        "DETAIL-AAA",          # audit_events.detail
        "AUDITACTOR-AAA",      # ⚠ audit_events.actor_email — see below
        "MAILGUNERROR-AAA",    # email_broadcast_recipients.error
    ):
        assert sentinel not in text, f"{sentinel} was exported but is redacted"


@pytest.mark.asyncio
async def test_audit_actor_email_is_never_exported(two_orgs):
    """⚠ ``audit_events.actor_email`` leaks an OPERATOR to a data subject.

    ~20 writers (``admin_orgs``, ``admin_features``, ``admin_subscriptions``,
    ``admin_rate_limit_overrides``, ...) set ``actor_email =
    current_user.email`` for a superadmin whose own ``users.org_id`` is a
    DIFFERENT org, while ``target_org_id`` is the tenant. Exporting org 42
    would therefore emit an operator's work email into a document handed to
    a data subject — the same Art. 15(4) third-party reasoning that drops
    ``ip_address``.

    Fenced at the column level as well as by sentinel, because a
    row-dependent "keep it for the org's own members" rule is what the spec
    originally asked for and is unimplementable against a static
    column-name redact set. ``actor_user_id`` survives, so the subject can
    still resolve their OWN members through the exported ``users`` rows.
    """
    columns = org_export_service._exported_columns("audit_events")

    assert "actor_email" not in columns, (
        "actor_email is exported; a superadmin's work email would ship "
        "inside a tenant's own data export"
    )
    # The identifier that makes it derivable for the org's own members must
    # survive — dropping both would lose the subject real information.
    assert "actor_user_id" in columns
    assert "event_type" in columns
    assert "target_org_name" in columns


@pytest.mark.asyncio
async def test_security_history_datetimes_ARE_exported(two_orgs):
    """⚠ ``password_changed_at`` / ``sessions_invalidated_at`` are datetimes,
    not credentials — the subject's own security history, Art. 15 material.
    An earlier brief wrongly listed them as forbidden."""
    async with two_orgs["factory"]() as db:
        columns = org_export_service._exported_columns("users")

    assert "password_changed_at" in columns
    assert "sessions_invalidated_at" in columns
    assert "password_hash" not in columns


def test_column_drift_every_secret_named_column_is_redacted_or_allowlisted():
    """Two INDEPENDENT sources: a naming heuristic vs the per-table redact
    sets. Neither is derived from the other, so a new secret-ish column on an
    included table cannot land silently."""
    offenders: list[str] = []
    for table_name in sorted(included_tables()):
        disposition = EXPORT_DISPOSITION[table_name]
        assert isinstance(disposition, Include)
        for column in Base.metadata.tables[table_name].columns:
            lowered = column.name.lower()
            if not any(p in lowered for p in SECRET_NAME_PATTERNS):
                continue
            if column.name in disposition.redact:
                continue
            if (table_name, column.name) in SECRET_NAME_ALLOWLIST:
                continue
            offenders.append(f"{table_name}.{column.name}")

    assert not offenders, (
        f"secret-named column(s) exported with no decision: {offenders}. "
        f"Either add to the table's redact set or to SECRET_NAME_ALLOWLIST "
        f"with a one-line justification."
    )


def test_secret_name_allowlist_has_no_dead_entries():
    """A stale allowlist entry silently pre-approves a future column."""
    dead = [
        f"{t}.{c}"
        for (t, c) in SECRET_NAME_ALLOWLIST
        if t not in Base.metadata.tables or c not in Base.metadata.tables[t].columns
    ]
    assert not dead, f"allowlist names non-existent columns: {dead}"


# ══ §5 — the ruled dispositions ═══════════════════════════════════════════


def test_ai_usage_ledger_reason_does_not_claim_user_attribution():
    """⚠ The table has NO ``user_id`` column, so there is no user
    attribution to be had. A reason string claiming otherwise documents a
    falsehood."""
    assert "user_id" not in Base.metadata.tables["ai_usage_ledger"].columns
    reason = EXPORT_DISPOSITION["ai_usage_ledger"].reason
    assert "behavioural" not in reason.lower()
    assert "identifiable" not in reason.lower()


def test_tag_dictionary_contributors_is_excluded():
    """The one table where exporting would be an ACTIVE privacy breach."""
    assert not EXPORT_DISPOSITION["tag_dictionary_contributors"].included


# ⚠ The 13 tables §5 ruled OUT of a data-subject export. Held as a literal on
# purpose: this is the ruling, restated independently of the registry.
RULED_EXCLUDED: frozenset[str] = frozenset(
    {
        "announcements",
        "api_tokens",
        "email_broadcasts",
        "merchant_dictionary",
        "org_data_reset_locks",
        "org_feature_overrides",
        "plans",
        "rate_limit_overrides",
        "role_permissions",
        "roles",
        "system_settings",
        "tag_dictionary",
        "tag_dictionary_contributors",
    }
)


def test_the_excluded_set_is_exactly_the_ruled_thirteen():
    """⚠ A DIRECT disclosure fence on the Exclude side.

    Flipping a table from Exclude to Include reddened only *incidentally*
    before this — via a fixture guard and the erasure-parity list, neither of
    which is about disclosure. So the one mutation that turns platform-global
    operator data into something shipped to a tenant had no test that was
    actually about that.

    Both directions: a new Exclude also has to be ruled, since silently
    withholding data is the omission drift the whole subsystem exists to
    prevent.
    """
    actual = set(excluded_reasons())

    assert actual == RULED_EXCLUDED, (
        f"the excluded set drifted from §5's ruling: "
        f"newly_excluded={sorted(actual - RULED_EXCLUDED)} "
        f"newly_included={sorted(RULED_EXCLUDED - actual)}. Every change here "
        f"is a disclosure decision and needs a §5 ruling, not just an edit."
    )
    assert included_tables() == set(Base.metadata.tables) - RULED_EXCLUDED, (
        "every table that is not one of the ruled 13 must be INCLUDED — a new "
        "table is neither ruled in nor ruled out until §5 says so."
    )


def test_every_excluded_table_ships_a_reason_to_the_subject():
    """``excluded_reasons()`` lands in the header, so a data subject reads
    these strings. An empty or placeholder one tells them nothing."""
    for table, reason in sorted(excluded_reasons().items()):
        assert len(reason) > 20, f"{table}: reason too thin to be informative"
        assert "TODO" not in reason.upper(), f"{table}: placeholder reason"


def test_invitations_carry_no_live_capability_column():
    """§5 rules invitations INCLUDE-full. That is only safe while the table
    has no token: a token column would make a full export hand over a live
    org-join capability."""
    columns = set(Base.metadata.tables["invitations"].columns.keys())
    assert not {c for c in columns if "token" in c.lower()}
