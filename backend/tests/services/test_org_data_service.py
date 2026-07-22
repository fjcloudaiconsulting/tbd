"""Service-layer tests for L3.1 — org data reset.

Fixture mirrors test_admin_orgs_service.py: in-memory aiosqlite with
PRAGMA foreign_keys=ON so SQLite enforces FKs the way MySQL would.
"""
from __future__ import annotations

import datetime
from app._time import utcnow_naive
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.feature_override import OrgFeatureOverride
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportSourceFormat
from app.models.merchant_dictionary import MerchantDictionaryEntry
from app.models.forecast_plan import (
    ForecastItemType, ForecastPlan, ForecastPlanItem, ItemSource, PlanStatus,
)
from app.models.invitation import Invitation
from app.models.recurring import Frequency, RecurringTransaction
from app.models.settings import OrgSetting
from app.models.subscription import (
    BillingInterval, Plan, Subscription, SubscriptionStatus,
)
from app.models.tag import (
    Tag, TagDictionary, TagDictionaryContributor, TransactionTag,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import org_data_service


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


async def _seed_full_org(factory, *, name: str = "Acme") -> dict:
    """Seed an org plus one row in every wipe-list AND preserve-list table.

    Returns ``{"org_id": int, "owner_id": int}``.
    """
    async with factory() as db:
        plan = (
            await db.execute(select(Plan).where(Plan.slug == "free"))
        ).scalar_one_or_none()
        if plan is None:
            plan = Plan(slug="free", name="Free")
            db.add(plan)
            await db.commit()

        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.commit()

        owner = User(
            org_id=org.id, username=f"{name}_owner",
            email=f"{name}_owner@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id, username=f"{name}_member",
            email=f"{name}_member@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([owner, member])
        await db.commit()

        sub = Subscription(
            org_id=org.id, plan_id=plan.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_start=datetime.date.today(),
            trial_end=datetime.date.today() + datetime.timedelta(days=14),
        )
        db.add(sub)

        atype = AccountType(org_id=org.id, name="Checking", slug=f"checking-{name}")
        db.add(atype)
        await db.commit()

        account = Account(
            org_id=org.id, account_type_id=atype.id,
            name="Main", balance=Decimal("100.00"),
        )
        master = Category(
            org_id=org.id, name="Food", slug=f"food-{name}",
            type=CategoryType.EXPENSE,
        )
        db.add_all([account, master])
        await db.commit()

        db.add(CcCyclePayment(
            account_id=account.id,
            period_anchor_year=2099,
            period_anchor_month=1,
            amount=Decimal("100.00"),
        ))
        await db.commit()

        # import_batches: account_id → accounts.id with no ON DELETE CASCADE.
        # Must be wiped before accounts; seeded here so tests cover the path.
        batch = ImportBatch(
            org_id=org.id,
            account_id=account.id,
            source_format=ImportSourceFormat.CSV,
            file_name="seed.csv",
            created_by_user_id=owner.id,
            status=ImportBatchStatus.CLOSED,
        )
        db.add(batch)
        await db.commit()

        sub_cat = Category(
            org_id=org.id, parent_id=master.id, name="Groceries",
            slug=f"groceries-{name}", type=CategoryType.EXPENSE,
        )
        db.add(sub_cat)
        bp = BillingPeriod(
            org_id=org.id,
            start_date=datetime.date.today().replace(day=1),
            end_date=None,
        )
        db.add(bp)
        await db.commit()

        recurring = RecurringTransaction(
            org_id=org.id, account_id=account.id, category_id=master.id,
            description="Rent", amount=Decimal("1500.00"),
            type="expense", frequency=Frequency.MONTHLY,
            next_due_date=datetime.date.today(),
        )
        db.add(recurring)
        await db.commit()

        tx = Transaction(
            org_id=org.id, account_id=account.id, category_id=master.id,
            recurring_id=recurring.id,
            description="Lunch", amount=Decimal("12.34"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=datetime.date.today(),
            settled_date=datetime.date.today(),
        )
        budget = Budget(
            org_id=org.id, category_id=master.id,
            amount=Decimal("400.00"),
            period_start=datetime.date.today().replace(day=1),
        )
        plan_row = ForecastPlan(
            org_id=org.id, billing_period_id=bp.id, status=PlanStatus.ACTIVE,
        )
        setting = OrgSetting(
            org_id=org.id, key=f"{name}_setting", value="x",
        )
        db.add_all([tx, budget, plan_row, setting])
        await db.commit()

        plan_item = ForecastPlanItem(
            plan_id=plan_row.id, org_id=org.id, category_id=master.id,
            type=ForecastItemType.EXPENSE, source=ItemSource.MANUAL,
            planned_amount=Decimal("400.00"),
        )
        invite = Invitation(
            org_id=org.id, email=f"invitee_{name}@acme.io",
            role=Role.MEMBER, open_email=f"invitee_{name}@acme.io",
            created_by=owner.id,
            expires_at=utcnow_naive() + datetime.timedelta(days=7),
        )
        rule = CategoryRule(
            org_id=org.id,
            normalized_token=f"TEST{name.upper()}",
            raw_description_seen=f"POS {name} *0001",
            category_id=master.id,
            match_count=1,
            source=RuleSource.USER_PICK,
        )
        override = OrgFeatureOverride(
            org_id=org.id,
            feature_key="ai.budget",
            value=False,
            set_by=owner.id,
        )
        db.add_all([plan_item, invite, rule, override])
        await db.commit()

        # Tags: one local tag attached to the seeded transaction, plus a
        # dictionary entry the org has contributed to. Lets the wipe
        # tests assert the count decrements + cascade behaviour.
        tag = Tag(
            org_id=org.id,
            name="insurance",
            name_normalized="insurance",
            created_by_user_id=owner.id,
        )
        db.add(tag)
        await db.flush()
        db.add(TransactionTag(transaction_id=tx.id, tag_id=tag.id))

        dict_tag = (
            await db.execute(
                select(TagDictionary).where(
                    TagDictionary.name_normalized == "insurance"
                )
            )
        ).scalar_one_or_none()
        if dict_tag is None:
            dict_tag = TagDictionary(
                name_normalized="insurance",
                contributor_org_count=0,
                usage_count=0,
                is_seed=False,
            )
            db.add(dict_tag)
            await db.flush()
        # Bump the count to reflect this org's contribution.
        dict_tag.contributor_org_count += 1
        db.add(TagDictionaryContributor(
            dictionary_tag_id=dict_tag.id,
            contributor_org_id=org.id,
        ))
        await db.commit()

        return {"org_id": org.id, "owner_id": owner.id}


async def _count(db: AsyncSession, model, **filt) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in filt.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await db.scalar(stmt)) or 0


# ── wipe_org_data ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wipe_clears_all_org_scoped_data(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        counts = await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    expected_keys = {
        "transactions", "forecast_plan_items", "budgets",
        "recurring_transactions", "forecast_plans", "billing_periods",
        "import_batches", "accounts", "account_types", "category_rules",
        "categories", "tags", "transaction_tags", "tag_dictionary_contributors",
        "cc_cycle_payments",
    }
    assert set(counts.keys()) == expected_keys
    for key, n in counts.items():
        # transaction_tags is wiped by CASCADE when transactions are
        # deleted earlier in the same wipe pass, so the explicit DELETE
        # at the end has no work to do. Tolerate a 0 there; the
        # assertion below confirms the join rows are actually gone.
        if key == "transaction_tags":
            assert n >= 0
            continue
        assert n >= 1, f"expected >=1 row deleted from {key}, got {n}"

    async with session_factory() as db:
        for model in (Transaction, ForecastPlanItem, Budget, RecurringTransaction,
                      ForecastPlan, BillingPeriod, ImportBatch, Account, AccountType,
                      CategoryRule, Category, Tag):
            assert await _count(db, model, org_id=seeded["org_id"]) == 0, (
                f"{model.__name__} not wiped"
            )
        # cc_cycle_payments has no org_id column, so it can't be checked
        # via the loop above; assert it's globally empty (the fixture
        # only ever seeds one org's worth in this test).
        assert await _count(db, CcCyclePayment) == 0, "CcCyclePayment not wiped"
        # Contributor rows for this org are gone too.
        assert (await db.scalar(
            select(func.count()).select_from(TagDictionaryContributor).where(
                TagDictionaryContributor.contributor_org_id == seeded["org_id"]
            )
        )) == 0
        # Join rows are gone (asserted independently of the rowcount).
        assert (await db.execute(select(TransactionTag))).all() == []


@pytest.mark.asyncio
async def test_wipe_preserves_org_shell(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    async with session_factory() as db:
        assert await _count(db, Organization, id=seeded["org_id"]) == 1
        assert await _count(db, User, org_id=seeded["org_id"]) == 2
        assert await _count(db, Subscription, org_id=seeded["org_id"]) == 1
        assert await _count(db, OrgSetting, org_id=seeded["org_id"]) == 1
        assert await _count(db, OrgFeatureOverride, org_id=seeded["org_id"]) == 1
        assert await _count(db, Invitation, org_id=seeded["org_id"]) == 1


@pytest.mark.asyncio
async def test_wipe_does_not_touch_merchant_dictionary(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        db.add(MerchantDictionaryEntry(
            normalized_token="LIDL", category_slug="groceries",
            is_seed=True, vote_count=0,
        ))
        await db.commit()

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    async with session_factory() as db:
        rows = (await db.execute(
            select(MerchantDictionaryEntry)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].normalized_token == "LIDL"


@pytest.mark.asyncio
async def test_wipe_does_not_touch_other_orgs(session_factory):
    target = await _seed_full_org(session_factory, name="Target")
    keep = await _seed_full_org(session_factory, name="Keep")

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=target["org_id"])
        await db.commit()

    async with session_factory() as db:
        for model in (Transaction, Budget, Account, AccountType, Category,
                      CategoryRule, BillingPeriod, RecurringTransaction,
                      ForecastPlan, ForecastPlanItem):
            assert await _count(db, model, org_id=keep["org_id"]) >= 1, (
                f"{model.__name__} for keep org unexpectedly wiped"
            )


@pytest.mark.asyncio
async def test_wipe_handles_categories_with_parent_id(session_factory):
    """Self-FK on categories.parent_id requires a parent_id-null trick
    before bulk DELETE. If broken, MySQL strict FK refuses; SQLite with
    PRAGMA foreign_keys=ON does the same."""
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        counts = await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()
    assert counts["categories"] == 2  # master + sub


@pytest.mark.asyncio
async def test_wipe_idempotent(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()
    async with session_factory() as db:
        second = await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()
    assert all(n == 0 for n in second.values())


# ── reset_org_data ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_returns_counts_and_wipes_data(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        # reset_org_data commits per batch internally; the outer commit
        # here is a no-op but kept for symmetry with the test pattern.
        counts = await org_data_service.reset_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    # The contract widened in the L3.4 follow-up: `reset_org_data` now
    # also re-seeds system defaults after the wipe and reports those
    # counts as `seeded_account_types` and `seeded_categories`.
    expected_keys = {
        "transactions", "forecast_plan_items", "budgets",
        "recurring_transactions", "forecast_plans", "billing_periods",
        "import_batches", "accounts", "account_types", "category_rules",
        "categories", "tags", "transaction_tags", "tag_dictionary_contributors",
        "cc_cycle_payments",
        "seeded_account_types", "seeded_categories",
    }
    assert set(counts.keys()) == expected_keys

    async with session_factory() as db:
        # Org shell still alive (wrapper didn't accidentally call cascade).
        assert await _count(db, Organization, id=seeded["org_id"]) == 1


@pytest.mark.asyncio
async def test_reset_reseeds_system_defaults_after_wipe(session_factory):
    """The L3.4 follow-up gap: post-reset, the org must look like a freshly
    registered org (system account types, system master + child categories,
    Transfer category) instead of an empty shell.
    """
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    # Pre-reset: verify the seeded org has *non-default* shape (otherwise
    # the assertions below trivially pass on the seed alone).
    async with session_factory() as db:
        # The fixture inserts 1 system + 1 user account type plus 1 master
        # + 1 child category. After reset we expect to see only system
        # account types and system categories — the user-added ones go.
        pre_user_at = await db.scalar(
            select(func.count()).select_from(AccountType).where(
                AccountType.org_id == org_id,
                AccountType.is_system.is_(False),
            )
        )
        assert pre_user_at >= 1, "fixture should seed at least one user account type"

    async with session_factory() as db:
        counts = await org_data_service.reset_org_data(db, org_id=org_id)

    # The seed inserted SOMETHING — non-zero counts confirm the re-seed ran.
    assert counts["seeded_account_types"] > 0
    assert counts["seeded_categories"] > 0

    async with session_factory() as db:
        # After reset, only system rows survive in the per-org tables.
        all_at = (await db.scalars(
            select(AccountType).where(AccountType.org_id == org_id)
        )).all()
        assert len(all_at) > 0
        assert all(at.is_system for at in all_at)

        all_cats = (await db.scalars(
            select(Category).where(Category.org_id == org_id)
        )).all()
        assert len(all_cats) > 0
        assert all(cat.is_system for cat in all_cats)
        # The Transfer system category specifically must be present.
        transfer = await db.scalar(
            select(Category).where(
                Category.org_id == org_id,
                Category.slug == "transfer",
                Category.is_system.is_(True),
            )
        )
        assert transfer is not None, "Transfer system category not re-seeded"


@pytest.mark.asyncio
async def test_seed_org_defaults_is_idempotent(session_factory):
    """``seed_org_defaults`` is keyed by ``(org_id, slug, is_system=True)``
    and must skip existing rows. Calling it twice without a wipe in
    between must not duplicate, and the second call's reported counts
    must be zero.
    """
    from app.services.org_bootstrap_service import seed_org_defaults

    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    # First call: ``_seed_full_org`` already inserted some system rows
    # (matching the registration shape — see fixture). The seed should
    # find them and only insert what's missing.
    async with session_factory() as db:
        first = await seed_org_defaults(db, org_id=org_id)
        await db.commit()

    async with session_factory() as db:
        second = await seed_org_defaults(db, org_id=org_id)
        await db.commit()
    # Second call: nothing missing, nothing inserted.
    assert second == {"account_types": 0, "categories": 0}

    # Row counts unchanged between the two calls.
    async with session_factory() as db:
        at_count = await db.scalar(
            select(func.count()).select_from(AccountType).where(AccountType.org_id == org_id)
        )
        cat_count = await db.scalar(
            select(func.count()).select_from(Category).where(Category.org_id == org_id)
        )
    # Sanity: the first call did insert rows (or the fixture already
    # had them all). Either way the contract is non-negative + stable.
    assert first["account_types"] >= 0
    assert first["categories"] >= 0
    assert at_count >= 1
    assert cat_count >= 1


@pytest.mark.asyncio
async def test_reset_end_state_is_stable_across_repeats(session_factory):
    """Repeated resets must leave the org in the same shape every
    time. Each reset wipes (including system rows) and re-seeds, so
    every reset's seed counts are non-zero — but the final row counts
    must be identical to the first reset's final state.
    """
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        await org_data_service.reset_org_data(db, org_id=org_id)

    async with session_factory() as db:
        first_at = await db.scalar(
            select(func.count()).select_from(AccountType).where(AccountType.org_id == org_id)
        )
        first_cat = await db.scalar(
            select(func.count()).select_from(Category).where(Category.org_id == org_id)
        )

    # Run reset two more times and confirm the row counts are stable.
    async with session_factory() as db:
        await org_data_service.reset_org_data(db, org_id=org_id)
    async with session_factory() as db:
        await org_data_service.reset_org_data(db, org_id=org_id)

    async with session_factory() as db:
        third_at = await db.scalar(
            select(func.count()).select_from(AccountType).where(AccountType.org_id == org_id)
        )
        third_cat = await db.scalar(
            select(func.count()).select_from(Category).where(Category.org_id == org_id)
        )
    assert third_at == first_at
    assert third_cat == first_cat


@pytest.mark.asyncio
async def test_admin_delete_still_uses_unbatched_wipe_path(session_factory):
    """Regression: ``admin_orgs_service.delete_org_cascade`` must keep
    using ``wipe_org_data`` (single transaction, no per-batch commit,
    no re-seed) — NOT the new ``reset_org_data`` path. A change to the
    self-service reset path must not bleed into the admin delete path.
    """
    from app.services import admin_orgs_service
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        counts = await admin_orgs_service.delete_org_cascade(db, org_id=org_id)
        # The admin-delete contract is: caller commits. delete_org_cascade
        # uses the unbatched wipe path expecting one commit boundary,
        # which is exactly what this regression is asserting must NOT
        # have changed when reset_org_data was rewritten.
        await db.commit()

    # delete_org_cascade returns its own merged dict including the
    # wipe table counts AND the org-shell counts (org_settings,
    # subscriptions, users, organization). Critically, it must NOT
    # include the seed keys — admin delete does not re-seed a tomb.
    assert "seeded_account_types" not in counts
    assert "seeded_categories" not in counts

    # The org itself is gone (admin-delete cascade ran to completion).
    async with session_factory() as db:
        assert await _count(db, Organization, id=org_id) == 0


# -- Tags lifecycle (Correction 2 + Correction 3) --------------------------


@pytest.mark.asyncio
async def test_wipe_clears_tags_and_join_rows(session_factory):
    """Self-service reset must wipe tags + transaction_tags + the
    contributor rows for the org. Without this, tags survive when the
    user runs ``/api/v1/orgs/data/reset``.
    """
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=org_id)
        await db.commit()

    async with session_factory() as db:
        assert await _count(db, Tag, org_id=org_id) == 0
        # The transaction_tags rows tied to this org's tags are gone.
        # (We can only filter by tag_id since transaction_tags has no
        # org_id column; if all tags are deleted, the join rows must
        # also be gone.)
        join_rows = (await db.execute(select(TransactionTag))).all()
        assert join_rows == []
        # Contributor rows for this org are removed.
        assert (await db.scalar(
            select(func.count()).select_from(TagDictionaryContributor).where(
                TagDictionaryContributor.contributor_org_id == org_id
            )
        )) == 0


@pytest.mark.asyncio
async def test_wipe_decrements_dictionary_count_for_contributing_org(
    session_factory,
):
    """Correction 3 invariant: ``contributor_org_count`` matches
    ``COUNT(DISTINCT contributor_org_id)`` after an org goes away. The
    fixture seeds one contributor for "insurance"; wipe must drop the
    count back to 0.
    """
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        before = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        assert before.contributor_org_count == 1

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=org_id)
        await db.commit()

    async with session_factory() as db:
        after = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        # Count went from 1 to 0 because the only contributor was wiped.
        assert after.contributor_org_count == 0


@pytest.mark.asyncio
async def test_wipe_decrement_only_touches_dict_tags_org_contributed(
    session_factory,
):
    """The decrement must apply ONLY to the dictionary tags this org
    contributed to, never to other dictionary entries.
    """
    seeded_a = await _seed_full_org(session_factory, name="A")
    seeded_b = await _seed_full_org(session_factory, name="B")

    # Add a separate dictionary tag that ONLY org A contributed to.
    async with session_factory() as db:
        only_a_dict = TagDictionary(
            name_normalized="only-a",
            contributor_org_count=1,
            usage_count=0,
            is_seed=False,
        )
        db.add(only_a_dict)
        await db.flush()
        db.add(TagDictionaryContributor(
            dictionary_tag_id=only_a_dict.id,
            contributor_org_id=seeded_a["org_id"],
        ))
        await db.commit()

    # Wipe org B; org A's "only-a" count must remain at 1.
    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded_b["org_id"])
        await db.commit()

    async with session_factory() as db:
        only_a_after = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "only-a"
            )
        )).scalar_one()
        assert only_a_after.contributor_org_count == 1


@pytest.mark.asyncio
async def test_admin_delete_clears_tags_via_cascade(session_factory):
    """Admin delete must cascade through tags so the FK chain doesn't
    block ``delete_org_cascade``. Combined with the model-level
    ``ondelete="CASCADE"`` on Tag.org_id, this gives belt-and-braces
    coverage.
    """
    from app.services import admin_orgs_service
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        await admin_orgs_service.delete_org_cascade(db, org_id=org_id)
        await db.commit()

    async with session_factory() as db:
        assert await _count(db, Organization, id=org_id) == 0
        # Tags for the deleted org are gone.
        assert (await db.scalar(
            select(func.count()).select_from(Tag).where(Tag.org_id == org_id)
        )) == 0
        # Contributor rows for the deleted org are gone (CASCADE on
        # contributor_org_id FK + the explicit pre-delete decrement
        # both contribute, but only one of them needs to fire for the
        # count to be zero).
        assert (await db.scalar(
            select(func.count()).select_from(TagDictionaryContributor).where(
                TagDictionaryContributor.contributor_org_id == org_id
            )
        )) == 0


@pytest.mark.asyncio
async def test_admin_delete_restores_k_anonymity_invariant(session_factory):
    """K-anonymity scenario: an "insurance" tag with 3 contributors
    sits AT the floor (3) and would surface in suggestions. After we
    delete one contributor org, the count must drop to 2 (below the
    floor of 3) so the tag stops surfacing.
    """
    from app.services import admin_orgs_service
    from app.schemas.tag import SHARED_DICTIONARY_MIN_CONTRIBUTORS
    assert SHARED_DICTIONARY_MIN_CONTRIBUTORS == 3

    # Seed 3 orgs all contributing the same dictionary tag.
    seeded_orgs = []
    for name in ("a", "b", "c"):
        seeded = await _seed_full_org(session_factory, name=name)
        seeded_orgs.append(seeded)

    # Promote the dictionary entry to count == 3 to match the floor.
    async with session_factory() as db:
        dict_tag = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        # Each _seed_full_org call adds 1 contributor row. 3 orgs => 3.
        assert dict_tag.contributor_org_count == 3

    # Delete org A.
    async with session_factory() as db:
        await admin_orgs_service.delete_org_cascade(
            db, org_id=seeded_orgs[0]["org_id"]
        )
        await db.commit()

    async with session_factory() as db:
        dict_tag_after = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        assert dict_tag_after.contributor_org_count == 2
        # Below the k-anonymity floor: a suggestion query for an org
        # with share_tag_data on would now exclude this entry.
        assert (
            dict_tag_after.contributor_org_count
            < SHARED_DICTIONARY_MIN_CONTRIBUTORS
        )


@pytest.mark.asyncio
async def test_org_re_creation_re_increments_count_on_contribution(
    session_factory,
):
    """Edge case: deleting an org's contribution and then having a
    new org contribute the same tag re-increments the count. Orgs
    with the same name do not share IDs (the model has no uniqueness
    on Organization.name), so a new org always gets a new contributor
    row.
    """
    from app.services import admin_orgs_service
    seeded = await _seed_full_org(session_factory, name="First")
    org_id = seeded["org_id"]

    async with session_factory() as db:
        before = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        assert before.contributor_org_count == 1

    # Full admin delete (org row + users + everything) so the next
    # _seed_full_org call cannot collide on user email.
    async with session_factory() as db:
        await admin_orgs_service.delete_org_cascade(db, org_id=org_id)
        await db.commit()

    async with session_factory() as db:
        after_delete = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        assert after_delete.contributor_org_count == 0

    # Re-seed under a fresh name (model has no uniqueness on org name,
    # but the fixture keys user emails off ``name`` so reusing the
    # original name would collide). The new contribution increments
    # the count back.
    await _seed_full_org(session_factory, name="Reborn")

    async with session_factory() as db:
        re_count = (await db.execute(
            select(TagDictionary).where(
                TagDictionary.name_normalized == "insurance"
            )
        )).scalar_one()
        assert re_count.contributor_org_count == 1


# -- cc_cycle_payments (Credit Card Model V1, Slice 2) ---------------------


@pytest.mark.asyncio
async def test_reset_wipes_cc_cycle_payments(session_factory):
    """reset_org_data must delete cc_cycle_payments via the subquery-
    scoped delete (no org_id column, so _batch_delete_by_pk is unusable)."""
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        before = await _count(db, CcCyclePayment)
        assert before >= 1

    async with session_factory() as db:
        counts = await org_data_service.reset_org_data(db, org_id=seeded["org_id"])

    assert counts["cc_cycle_payments"] >= 1
    async with session_factory() as db:
        assert await _count(db, CcCyclePayment) == 0
