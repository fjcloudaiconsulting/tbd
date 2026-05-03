"""execute_import learns from each confirmed row + emits aggregate metric.

Covers Task 7 of L3.10.

  Accept (row.category_id == row.suggested_category_id):
    -> learn_from_choice(source="user_pick"); bump shared vote if org opted in.
  Override (row.category_id != row.suggested_category_id, or no suggestion):
    -> learn_from_choice(source="user_edit"); do NOT bump shared vote.
  Transfer rows: skip both.
  Aggregate metric: one smart_rules.import_executed event after the loop;
                    smart_rules.miss once per UNIQUE missed token.
"""
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry
from app.models.settings import OrgSetting
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.services import import_service


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _r):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession, *, share: bool = False) -> dict:
    org = Organization(name="X", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    groc = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        is_system=True, type=CategoryType.EXPENSE,
    )
    rest = Category(
        org_id=org.id, name="Restaurants", slug="restaurants",
        is_system=True, type=CategoryType.EXPENSE,
    )
    db.add_all([groc, rest])
    atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(atype)
    await db.flush()
    acct = Account(
        org_id=org.id, account_type_id=atype.id, name="Checking",
        balance=Decimal("0"),
    )
    db.add(acct)
    db.add(MerchantDictionaryEntry(
        normalized_token="LIDL", category_slug="groceries",
        is_seed=True, vote_count=0,
    ))
    if share:
        db.add(OrgSetting(org_id=org.id, key="share_merchant_data", value="true"))
    await db.commit()
    return {
        "org_id": org.id,
        "account_id": acct.id,
        "account_type_id": atype.id,
        "groceries_id": groc.id,
        "restaurants_id": rest.id,
    }


async def test_accept_learns_user_pick_and_bumps_shared(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, share=True)
    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["restaurants_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999", amount=Decimal("12.50"), type="expense",
            category_id=seed["groceries_id"],
            suggested_category_id=seed["groceries_id"],
            suggestion_source="shared_dictionary",
        )],
    )
    await import_service.execute_import(db_session, org_id=seed["org_id"], body=body)

    rule = (await db_session.execute(select(CategoryRule))).scalar_one()
    assert rule.source == RuleSource.USER_PICK
    assert rule.category_id == seed["groceries_id"]

    entry = (await db_session.execute(select(MerchantDictionaryEntry))).scalar_one()
    assert entry.vote_count == 1


async def test_override_learns_user_edit_no_shared_bump(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, share=True)
    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["restaurants_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999", amount=Decimal("12.50"), type="expense",
            category_id=seed["restaurants_id"],
            suggested_category_id=seed["groceries_id"],
            suggestion_source="shared_dictionary",
        )],
    )
    await import_service.execute_import(db_session, org_id=seed["org_id"], body=body)

    rule = (await db_session.execute(select(CategoryRule))).scalar_one()
    assert rule.source == RuleSource.USER_EDIT
    assert rule.category_id == seed["restaurants_id"]

    entry = (await db_session.execute(select(MerchantDictionaryEntry))).scalar_one()
    assert entry.vote_count == 0


async def test_accept_without_opt_in_does_not_bump_shared(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, share=False)
    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["restaurants_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999", amount=Decimal("12.50"), type="expense",
            category_id=seed["groceries_id"],
            suggested_category_id=seed["groceries_id"],
            suggestion_source="shared_dictionary",
        )],
    )
    await import_service.execute_import(db_session, org_id=seed["org_id"], body=body)
    entry = (await db_session.execute(select(MerchantDictionaryEntry))).scalar_one()
    assert entry.vote_count == 0


async def test_transfer_row_does_not_learn(db_session: AsyncSession) -> None:
    """Paired rows (action='pair_with_existing') must NOT trigger smart-rules
    learning. The new ORM Transaction has linked_transaction_id set after
    _link_pair, so should_skip_learning (now via is_transfer_leg) returns
    True and the learn-from-choice block is skipped.
    """
    seed = await _seed(db_session, share=False)
    # Build a partner on a SEPARATE account (transfers require different
    # accounts). Reuse the existing checking account as source; create a
    # second account for the partner leg.
    other_acct = Account(
        org_id=seed["org_id"], account_type_id=seed["account_type_id"],
        name="Savings", balance=Decimal("0"), currency="EUR",
    )
    db_session.add(other_acct)
    await db_session.flush()
    # Make sure the source account also has a currency for the pair partner check.
    src_acct = (await db_session.execute(
        select(Account).where(Account.id == seed["account_id"])
    )).scalar_one()
    src_acct.currency = "EUR"
    transfer_cat = Category(
        org_id=seed["org_id"], name="Transfer", slug="transfer",
        is_system=True, type=CategoryType.BOTH,
    )
    db_session.add(transfer_cat)
    await db_session.flush()
    # Un-linked income leg on the partner account.
    partner = Transaction(
        org_id=seed["org_id"], account_id=other_acct.id,
        category_id=transfer_cat.id, description="incoming-xfer",
        amount=Decimal("12.50"),
        type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 5, 1),
        settled_date=datetime.date(2026, 5, 1),
    )
    db_session.add(partner)
    await db_session.commit()

    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=transfer_cat.id,
        rows=[ImportConfirmRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999",  # would normally learn → "LIDL"
            amount=Decimal("12.50"), type="expense",
            category_id=transfer_cat.id,
            action="pair_with_existing",
            pair_with_transaction_id=partner.id,
            recategorize=False,
            # Echo a suggestion so we'd see learning if it ran.
            suggested_category_id=seed["groceries_id"],
            suggestion_source="shared_dictionary",
        )],
    )
    await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )

    # No CategoryRule should have been written for the LIDL token; the
    # paired row's linked_transaction_id is non-None so should_skip_learning
    # short-circuits learn_from_choice and the source_split miss path.
    rules = (await db_session.execute(select(CategoryRule))).scalars().all()
    assert rules == []


async def test_aggregate_metric_emitted_with_correct_shape(db_session: AsyncSession) -> None:
    """Architect-mandated aggregate metric:
      - exactly one smart_rules.import_executed event per import
      - exactly one smart_rules.miss event per UNIQUE missed normalized_token
      - the aggregate event has rows_total, learned_count, accepted_count,
        overridden_count, source_split, miss_count
    """
    seed = await _seed(db_session, share=False)
    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["restaurants_id"],
        rows=[
            # accept
            ImportConfirmRow(
                row_number=1, date=datetime.date(2026, 5, 1),
                description="POS LIDL *0001", amount=Decimal("10"), type="expense",
                category_id=seed["groceries_id"],
                suggested_category_id=seed["groceries_id"],
                suggestion_source="shared_dictionary",
            ),
            # override
            ImportConfirmRow(
                row_number=2, date=datetime.date(2026, 5, 1),
                description="POS LIDL *0002", amount=Decimal("11"), type="expense",
                category_id=seed["restaurants_id"],
                suggested_category_id=seed["groceries_id"],
                suggestion_source="shared_dictionary",
            ),
            # default -> unique miss token "NOVEL CAFE"
            ImportConfirmRow(
                row_number=3, date=datetime.date(2026, 5, 1),
                description="POS NOVEL CAFE *0003", amount=Decimal("4"), type="expense",
                category_id=seed["restaurants_id"],
                suggested_category_id=None,
                suggestion_source="default",
            ),
        ],
    )

    with patch.object(
        import_service.logger, "ainfo", new_callable=AsyncMock
    ) as mock_log:
        await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    events = mock_log.call_args_list
    aggregate_calls = [c for c in events if c.args and c.args[0] == "smart_rules.import_executed"]
    miss_calls = [c for c in events if c.args and c.args[0] == "smart_rules.miss"]

    assert len(aggregate_calls) == 1
    agg_kwargs = aggregate_calls[0].kwargs
    assert agg_kwargs["org_id"] == seed["org_id"]
    assert agg_kwargs["rows_total"] == 3
    assert agg_kwargs["learned_count"] == 3
    assert agg_kwargs["accepted_count"] == 1
    assert agg_kwargs["overridden_count"] == 1  # row 2 has a suggestion but user overrode
    assert agg_kwargs["miss_count"] == 1
    # source_split keys come from suggestion_source labels on confirmed rows
    assert agg_kwargs["source_split"]["shared_dictionary"] == 2
    assert agg_kwargs["source_split"]["default"] == 1

    # Exactly one miss event for the unique normalized token.
    assert len(miss_calls) == 1
    miss_kwargs = miss_calls[0].kwargs
    assert miss_kwargs["org_id"] == seed["org_id"]
    assert miss_kwargs["normalized_token"]  # non-empty (will be "NOVEL CAFE")


async def test_learn_failure_does_not_fail_the_import(
    db_session: AsyncSession,
) -> None:
    """If learn_from_choice raises, the imported transaction is preserved
    and the failure is logged (not propagated to the caller).

    PR-C review high-severity regression: the learn block must NOT roll back
    successfully imported rows that share the outer transaction. The fix
    isolates learn in a nested savepoint so a learn-side failure rolls back
    only the savepoint, not the outer commit.
    """
    seed = await _seed(db_session, share=False)
    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["restaurants_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999", amount=Decimal("12.50"), type="expense",
            category_id=seed["groceries_id"],
            suggested_category_id=seed["groceries_id"],
            suggestion_source="shared_dictionary",
        )],
    )

    with patch(
        "app.services.import_service.learn_from_choice",
        new=AsyncMock(side_effect=RuntimeError("simulated learn failure")),
    ):
        result = await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    assert result.imported_count == 1
    assert result.error_count == 0  # learn failure does NOT surface as a row error

    # Imported row must still be in the DB — savepoint isolation prevents
    # the learn rollback from taking the import with it.
    persisted = (await db_session.execute(
        select(Transaction).where(
            Transaction.org_id == seed["org_id"],
            Transaction.description == "POS LIDL *9999",
        )
    )).scalars().all()
    assert len(persisted) == 1, (
        "imported row should survive learn failure; "
        "savepoint isolation regression"
    )


async def test_default_category_fallthrough_records_miss(
    db_session: AsyncSession,
) -> None:
    """The architect-mandated metric: when a row has NO suggestion AND the user
    leaves category_id None (relying on default_category_id), we must still
    record the missed normalized token. This was a pre-merge review fix.
    """
    seed = await _seed(db_session, share=False)
    body = ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["restaurants_id"],
        rows=[
            ImportConfirmRow(
                row_number=1, date=datetime.date(2026, 5, 1),
                description="POS NOVEL CAFE *0001", amount=Decimal("4.00"),
                type="expense",
                category_id=None,                       # user did NOT pick
                suggested_category_id=None,             # no suggestion
                suggestion_source="default",
            ),
        ],
    )

    with patch.object(
        import_service.logger, "ainfo", new_callable=AsyncMock
    ) as mock_log:
        result = await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    assert result.imported_count == 1
    miss_calls = [
        c for c in mock_log.call_args_list
        if c.args and c.args[0] == "smart_rules.miss"
    ]
    assert len(miss_calls) == 1
    assert miss_calls[0].kwargs["normalized_token"]  # non-empty

    aggregate_calls = [
        c for c in mock_log.call_args_list
        if c.args and c.args[0] == "smart_rules.import_executed"
    ]
    assert len(aggregate_calls) == 1
    agg = aggregate_calls[0].kwargs
    assert agg["miss_count"] == 1
    assert agg["learned_count"] == 0  # user didn't pick → no learn
    assert agg["source_split"]["default"] == 1
