"""Detector 1 + Detector 2 wiring in build_preview (PR-C C2).

Covers spec §3.1 / §3.2:
  Detector 1 = find_duplicate_of_linked_leg → is_duplicate_of_linked_leg=True
              + duplicate_candidate populated + default_action_drop=True.
  Detector 2 = find_match_candidates → transfer_match_action set per
              candidate count and date proximity.
  Precedence: Detector 1 wins; Detector 2 is skipped on the same row.
  Summary counters in ImportPreviewResponse mirror per-row actions.
  Telemetry: one import.preview.matched event per preview.
"""
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import import_service
from app.services.import_parser import ParsedRow


@pytest_asyncio.fixture
async def db_session():
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_two_accounts(db: AsyncSession) -> dict:
    """Seed an org, two EUR accounts (src=destination, dst=other), and a
    Transfer category. Returns dict with ids needed by tests.
    """
    org = Organization(name="X", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    atype = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True,
    )
    db.add(atype)
    await db.flush()
    src = Account(
        org_id=org.id, account_type_id=atype.id, name="Src",
        balance=Decimal("0"), currency="EUR",
    )
    dst = Account(
        org_id=org.id, account_type_id=atype.id, name="Dst",
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([src, dst])
    await db.flush()
    transfer_cat = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        is_system=True, type=CategoryType.BOTH,
    )
    db.add(transfer_cat)
    await db.commit()
    return {
        "org_id": org.id,
        "src_id": src.id,
        "dst_id": dst.id,
        "transfer_cat_id": transfer_cat.id,
    }


async def _add_linked_pair(
    db: AsyncSession,
    *,
    org_id: int,
    expense_account_id: int,
    income_account_id: int,
    category_id: int,
    amount: Decimal,
    when: datetime.date,
    is_imported: bool = False,
) -> tuple[Transaction, Transaction]:
    expense = Transaction(
        org_id=org_id, account_id=expense_account_id, category_id=category_id,
        description="link", amount=amount, type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=when, settled_date=when,
        is_imported=is_imported,
    )
    income = Transaction(
        org_id=org_id, account_id=income_account_id, category_id=category_id,
        description="link", amount=amount, type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=when, settled_date=when,
        is_imported=is_imported,
    )
    db.add_all([expense, income])
    await db.flush()
    expense.linked_transaction_id = income.id
    income.linked_transaction_id = expense.id
    await db.commit()
    return expense, income


async def _add_unlinked(
    db: AsyncSession,
    *,
    org_id: int,
    account_id: int,
    category_id: int,
    amount: Decimal,
    type: TransactionType,
    when: datetime.date,
    description: str = "u",
) -> Transaction:
    tx = Transaction(
        org_id=org_id, account_id=account_id, category_id=category_id,
        description=description, amount=amount, type=type,
        status=TransactionStatus.SETTLED, date=when, settled_date=when,
    )
    db.add(tx)
    await db.commit()
    await db.refresh(tx)
    return tx


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_detector_1_flags_duplicate_of_linked_leg(db_session: AsyncSession) -> None:
    """A CSV row matching an already-linked leg on the same account should be
    flagged is_duplicate_of_linked_leg + default_action_drop, with the
    duplicate_candidate populated.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Existing linked pair: expense leg on src (the destination of import).
    expense, _income = await _add_linked_pair(
        db_session,
        org_id=seed["org_id"],
        expense_account_id=seed["src_id"],
        income_account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"],
        amount=Decimal("10"),
        when=when,
        is_imported=False,  # synthetic leg from Op-3 convert-and-create
    )

    rows = [
        ParsedRow(
            row_number=1, date=when, description="ATM",
            amount=Decimal("10"), type="expense",
            counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"],
        account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].is_duplicate_of_linked_leg is True
    assert result.rows[0].default_action_drop is True
    assert result.rows[0].duplicate_candidate is not None
    assert result.rows[0].duplicate_candidate.id == expense.id
    assert result.rows[0].duplicate_candidate.account_id == seed["src_id"]
    assert result.rows[0].duplicate_candidate.existing_leg_is_imported is False
    # Detector 2 must NOT also fire on the same row.
    assert result.rows[0].transfer_match_action == "none"
    # Counters reflect Detector 1.
    assert result.duplicate_of_linked_count == 1
    assert result.auto_paired_count == 0


async def test_detector_2_same_day_single_match_pair_with(
    db_session: AsyncSession,
) -> None:
    """A single un-linked income leg on the other account on the same date
    yields action='pair_with' + same_day confidence.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Un-linked income on dst → CSV row is expense on src ⇒ pair candidate.
    other = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("25"),
        type=TransactionType.INCOME, when=when,
    )

    rows = [
        ParsedRow(
            row_number=1, date=when, description="POS COFFEE",
            amount=Decimal("25"), type="expense",
            counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"],
        account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].transfer_match_action == "pair_with"
    assert result.rows[0].transfer_match_confidence == "same_day"
    assert result.rows[0].pair_with_transaction_id == other.id
    assert result.rows[0].transfer_candidates == []
    assert result.rows[0].is_duplicate_of_linked_leg is False
    assert result.auto_paired_count == 1
    assert result.suggested_pair_count == 0
    assert result.multi_candidate_count == 0


async def test_detector_2_near_date_single_match_suggest_pair(
    db_session: AsyncSession,
) -> None:
    """A single match within ±3 days but not same-day yields
    action='suggest_pair' + near_date confidence.
    """
    seed = await _seed_two_accounts(db_session)
    csv_date = datetime.date(2026, 5, 3)
    other_date = datetime.date(2026, 5, 1)  # 2 days off
    other = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("40"),
        type=TransactionType.INCOME, when=other_date,
    )

    rows = [
        ParsedRow(
            row_number=1, date=csv_date, description="POS COFFEE",
            amount=Decimal("40"), type="expense",
            counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"],
        account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].transfer_match_action == "suggest_pair"
    assert result.rows[0].transfer_match_confidence == "near_date"
    assert result.rows[0].pair_with_transaction_id == other.id
    assert result.suggested_pair_count == 1
    assert result.auto_paired_count == 0


async def test_detector_2_multi_candidate_choose(
    db_session: AsyncSession,
) -> None:
    """≥2 candidates → action='choose_candidate', candidates list populated,
    pair_with_transaction_id None.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    a = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("15"),
        type=TransactionType.INCOME, when=when, description="A",
    )
    b = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("15"),
        type=TransactionType.INCOME, when=when, description="B",
    )

    rows = [
        ParsedRow(
            row_number=1, date=when, description="POS",
            amount=Decimal("15"), type="expense",
            counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"],
        account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].transfer_match_action == "choose_candidate"
    assert result.rows[0].transfer_match_confidence == "multi_candidate"
    assert result.rows[0].pair_with_transaction_id is None
    candidate_ids = {c.id for c in result.rows[0].transfer_candidates}
    assert candidate_ids == {a.id, b.id}
    # confidence per-candidate is same_day (both share the CSV date).
    for c in result.rows[0].transfer_candidates:
        assert c.confidence == "same_day"
        assert c.date_diff_days == 0
        assert c.account_id == seed["dst_id"]
        assert c.account_name == "Dst"
    assert result.multi_candidate_count == 1


async def test_detector_1_takes_precedence_over_detector_2(
    db_session: AsyncSession,
) -> None:
    """When both detectors could fire, only Detector 1 runs. Detector 2
    output is the 'none' default.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Detector 1 ammo: linked expense leg on src.
    await _add_linked_pair(
        db_session,
        org_id=seed["org_id"],
        expense_account_id=seed["src_id"],
        income_account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"],
        amount=Decimal("99"),
        when=when,
    )
    # Detector 2 ammo: un-linked income leg on dst that would otherwise pair.
    other = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("99"),
        type=TransactionType.INCOME, when=when,
    )

    rows = [
        ParsedRow(
            row_number=1, date=when, description="ATM",
            amount=Decimal("99"), type="expense",
            counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"],
        account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].is_duplicate_of_linked_leg is True
    assert result.rows[0].default_action_drop is True
    # Detector 2 must be silent on this row.
    assert result.rows[0].transfer_match_action == "none"
    assert result.rows[0].pair_with_transaction_id is None
    assert result.rows[0].transfer_candidates == []
    # Counters: only the duplicate-of-linked counter advances.
    assert result.duplicate_of_linked_count == 1
    assert result.auto_paired_count == 0
    assert result.suggested_pair_count == 0
    assert result.multi_candidate_count == 0
    # And the other un-linked candidate exists in the DB but was ignored.
    assert other.id is not None


async def test_summary_counters_match_per_row_actions_and_emit_metric(
    db_session: AsyncSession,
) -> None:
    """A 3-row preview: pair_with, suggest_pair, none → counters match;
    exactly one import.preview.matched event with the right shape.
    """
    seed = await _seed_two_accounts(db_session)
    same_day = datetime.date(2026, 5, 5)
    near_csv = datetime.date(2026, 5, 5)
    near_other = datetime.date(2026, 5, 3)
    # Row 1: same-day single match → pair_with
    auto_partner = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("11"),
        type=TransactionType.INCOME, when=same_day, description="P1",
    )
    # Row 2: near-date single match → suggest_pair
    suggest_partner = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("22"),
        type=TransactionType.INCOME, when=near_other, description="P2",
    )
    # Row 3: no match → none

    rows = [
        ParsedRow(
            row_number=1, date=same_day, description="auto-pair",
            amount=Decimal("11"), type="expense",
            counterparty=None, transaction_type=None,
        ),
        ParsedRow(
            row_number=2, date=near_csv, description="suggest-pair",
            amount=Decimal("22"), type="expense",
            counterparty=None, transaction_type=None,
        ),
        ParsedRow(
            row_number=3, date=same_day, description="lonely",
            amount=Decimal("999"), type="expense",
            counterparty=None, transaction_type=None,
        ),
    ]
    with patch.object(
        import_service.logger, "ainfo", new_callable=AsyncMock,
    ) as spy:
        result = await import_service.build_preview(
            db_session, org_id=seed["org_id"],
            account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
        )

    assert [r.transfer_match_action for r in result.rows] == [
        "pair_with", "suggest_pair", "none",
    ]
    assert result.rows[0].pair_with_transaction_id == auto_partner.id
    assert result.rows[1].pair_with_transaction_id == suggest_partner.id
    assert result.auto_paired_count == 1
    assert result.suggested_pair_count == 1
    assert result.multi_candidate_count == 0
    assert result.duplicate_of_linked_count == 0

    matched_calls = [
        c for c in spy.call_args_list
        if c.args and c.args[0] == "import.preview.matched"
    ]
    assert len(matched_calls) == 1, "exactly one matched-summary event per preview"
    kwargs = matched_calls[0].kwargs
    assert kwargs["org_id"] == seed["org_id"]
    assert kwargs["file_name"] == "t.csv"
    assert kwargs["auto_paired"] == 1
    assert kwargs["suggested"] == 1
    assert kwargs["multi_candidate"] == 0
    assert kwargs["duplicate_of_linked"] == 0


async def test_legacy_online_banking_string_no_longer_triggers_transfer_flag(
    db_session: AsyncSession,
) -> None:
    """The pre-PR-C heuristic on `transaction_type=='online banking'` is gone.
    With no DB candidates, the row is plain (action='none'), and counters
    are zero.
    """
    seed = await _seed_two_accounts(db_session)
    rows = [
        ParsedRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS COFFEE", amount=Decimal("3.50"),
            type="expense", counterparty=None, transaction_type="Online Banking",
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"],
        account_id=seed["src_id"], file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].transfer_match_action == "none"
    assert result.rows[0].is_duplicate_of_linked_leg is False
    assert result.rows[0].pair_with_transaction_id is None
    assert result.rows[0].transfer_candidates == []
    assert result.auto_paired_count == 0
    assert result.suggested_pair_count == 0
    assert result.multi_candidate_count == 0
    assert result.duplicate_of_linked_count == 0
