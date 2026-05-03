"""Detector 1 + Detector 2 wiring in build_preview (PR-C C2) +
execute_import confirm-branch wiring (PR-C C3).

Covers spec §3.1 / §3.2 / §3.3:
  Detector 1 = find_duplicate_of_linked_leg → is_duplicate_of_linked_leg=True
              + duplicate_candidate populated + default_action_drop=True.
  Detector 2 = find_match_candidates → transfer_match_action set per
              candidate count and date proximity.
  Precedence: Detector 1 wins; Detector 2 is skipped on the same row.
  Summary counters in ImportPreviewResponse mirror per-row actions.
  Telemetry: one import.preview.matched event per preview.
  C3 confirm: action="pair_with_existing" inserts + links atomically;
              action="drop_as_duplicate" revalidates server-side and skips.
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
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
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


# ── PR-C C3 confirm-branch tests ─────────────────────────────────────────────


async def test_confirm_pair_with_existing_creates_and_links_atomically(
    db_session: AsyncSession,
) -> None:
    """Happy path: action='pair_with_existing' inserts the new leg,
    locks the partner, and links both rows under a single per-row savepoint.
    Both legs end up linked; transfers.linked telemetry is emitted with
    source='import_pair'.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Partner: un-linked income on dst (the OTHER account).
    partner = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("50"),
        type=TransactionType.INCOME, when=when, description="incoming-xfer",
    )

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="ATM WITHDRAW", amount=Decimal("50"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="pair_with_existing",
            pair_with_transaction_id=partner.id,
            recategorize=False,  # already on Transfer category
        )],
    )

    with patch.object(
        import_service.logger, "ainfo", new_callable=AsyncMock,
    ) as spy:
        result = await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    assert result.imported_count == 0
    assert result.paired_count == 1
    assert result.dropped_duplicate_count == 0
    assert result.error_count == 0

    # Both legs are linked.
    new_leg = (await db_session.execute(
        select(Transaction).where(
            Transaction.account_id == seed["src_id"],
            Transaction.type == TransactionType.EXPENSE,
        )
    )).scalar_one()
    await db_session.refresh(partner)
    assert new_leg.linked_transaction_id == partner.id
    assert partner.linked_transaction_id == new_leg.id

    # Telemetry: transfers.linked with source='import_pair'.
    linked_calls = [
        c for c in spy.call_args_list
        if c.args and c.args[0] == "transfers.linked"
    ]
    assert len(linked_calls) == 1
    assert linked_calls[0].kwargs["source"] == "import_pair"
    assert linked_calls[0].kwargs["recategorized"] is False


async def test_confirm_pair_with_existing_atomicity_rollback(
    db_session: AsyncSession,
) -> None:
    """Force _link_pair to raise after _create_transaction_no_commit succeeds.
    The new CSV leg must NOT be present in the DB after the rollback (the
    per-row savepoint catches it). The partner must remain un-linked.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    partner = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("75"),
        type=TransactionType.INCOME, when=when, description="incoming-xfer",
    )

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="ATM WITHDRAW", amount=Decimal("75"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="pair_with_existing",
            pair_with_transaction_id=partner.id,
        )],
    )

    with patch(
        "app.services.import_service._link_pair",
        new=AsyncMock(side_effect=RuntimeError("simulated link failure")),
    ):
        result = await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    # Failure surfaces as a row error.
    assert result.paired_count == 0
    assert result.imported_count == 0
    assert result.error_count == 1
    assert "simulated link failure" in result.errors[0].error

    # The new CSV leg must NOT exist (savepoint rolled back).
    new_legs = (await db_session.execute(
        select(Transaction).where(
            Transaction.account_id == seed["src_id"],
            Transaction.description == "ATM WITHDRAW",
        )
    )).scalars().all()
    assert new_legs == [], "new CSV leg should be rolled back"

    # Partner remains un-linked.
    await db_session.refresh(partner)
    assert partner.linked_transaction_id is None


async def test_confirm_drop_as_duplicate_revalidates_server_side(
    db_session: AsyncSession,
) -> None:
    """Preview returned a duplicate-of-linked-leg candidate; before confirm,
    the candidate gets unpaired (so it would no longer match — it has no
    linked_transaction_id anymore). Confirm with action='drop_as_duplicate'
    must raise ConflictError ('re-preview'), surfaced as a row error.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Linked pair: expense leg on src. This is the would-be duplicate target.
    expense, income = await _add_linked_pair(
        db_session,
        org_id=seed["org_id"],
        expense_account_id=seed["src_id"],
        income_account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"],
        amount=Decimal("10"),
        when=when,
        is_imported=False,
    )

    # Between preview and confirm: unpair the candidate.
    expense.linked_transaction_id = None
    income.linked_transaction_id = None
    await db_session.commit()

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="ATM", amount=Decimal("10"),
            type="expense",
            action="drop_as_duplicate",
            duplicate_of_transaction_id=expense.id,
        )],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )

    assert result.dropped_duplicate_count == 0
    assert result.error_count == 1
    assert "re-preview" in result.errors[0].error.lower()


async def test_confirm_response_counters_match_actions(
    db_session: AsyncSession,
) -> None:
    """Mix of action='create', 'pair_with_existing', 'drop_as_duplicate' rows
    in one batch. Each counter increments correctly; sum + skipped + errors
    equals total submitted.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Partner for pair branch.
    partner = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("100"),
        type=TransactionType.INCOME, when=when, description="incoming-xfer",
    )
    # Linked-leg target for drop branch (preserve linked state through confirm).
    linked_expense, _linked_income = await _add_linked_pair(
        db_session,
        org_id=seed["org_id"],
        expense_account_id=seed["src_id"],
        income_account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"],
        amount=Decimal("20"),
        when=when,
    )

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[
            # plain create
            ImportConfirmRow(
                row_number=1, date=when,
                description="POS COFFEE", amount=Decimal("3.50"),
                type="expense",
                category_id=seed["transfer_cat_id"],
                action="create",
            ),
            # pair_with_existing
            ImportConfirmRow(
                row_number=2, date=when,
                description="ATM WITHDRAW", amount=Decimal("100"),
                type="expense",
                category_id=seed["transfer_cat_id"],
                action="pair_with_existing",
                pair_with_transaction_id=partner.id,
                recategorize=False,
            ),
            # drop_as_duplicate (still linked → revalidation passes).
            ImportConfirmRow(
                row_number=3, date=when,
                description="ATM REPLAY", amount=Decimal("20"),
                type="expense",
                action="drop_as_duplicate",
                duplicate_of_transaction_id=linked_expense.id,
            ),
        ],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )

    assert result.imported_count == 1
    assert result.paired_count == 1
    assert result.dropped_duplicate_count == 1
    assert result.skipped_count == 0
    assert result.error_count == 0
    # Counters sum to total submitted rows.
    total = (
        result.imported_count + result.paired_count
        + result.dropped_duplicate_count + result.skipped_count
        + result.error_count
    )
    assert total == len(body.rows)


async def test_confirm_pair_with_existing_rejects_pending_partner(
    db_session: AsyncSession,
) -> None:
    """Stale-preview test (PR-C review fix 3): partner became PENDING between
    preview and confirm. find_match_candidates only suggests SETTLED rows,
    so a partner whose status flipped after preview must be rejected at
    confirm to preserve the pair invariant.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    # Partner: un-linked SETTLED income on dst (eligible at preview-time).
    partner = await _add_unlinked(
        db_session,
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"], amount=Decimal("60"),
        type=TransactionType.INCOME, when=when, description="incoming-xfer",
    )

    # Between preview and confirm: partner flipped to PENDING.
    partner.status = TransactionStatus.PENDING
    await db_session.commit()

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="ATM WITHDRAW", amount=Decimal("60"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="pair_with_existing",
            pair_with_transaction_id=partner.id,
        )],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )

    # Surfaces as a row error; not paired.
    assert result.paired_count == 0
    assert result.imported_count == 0
    assert result.error_count == 1
    assert "re-preview" in result.errors[0].error.lower()

    # Partner remains un-linked.
    await db_session.refresh(partner)
    assert partner.linked_transaction_id is None
    # The new CSV leg must NOT exist (savepoint rolled back).
    new_legs = (await db_session.execute(
        select(Transaction).where(
            Transaction.account_id == seed["src_id"],
            Transaction.description == "ATM WITHDRAW",
        )
    )).scalars().all()
    assert new_legs == []


# ── IMM (import-mark-manual) create_transfer_pair branch ─────────────────────


async def test_confirm_create_transfer_pair_creates_both_legs_and_links(
    db_session: AsyncSession,
) -> None:
    """action='create_transfer_pair' creates the CSV leg + a synthetic
    partner leg on partner_account_id and links them. Counts once toward
    paired_count; transfers.linked emitted with source='import_create_pair'.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="TO SAVINGS", amount=Decimal("80"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="create_transfer_pair",
            partner_account_id=seed["dst_id"],
            recategorize=False,
        )],
    )

    with patch.object(
        import_service.logger, "ainfo", new_callable=AsyncMock,
    ) as spy:
        result = await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    assert result.imported_count == 0
    assert result.paired_count == 1
    assert result.dropped_duplicate_count == 0
    assert result.error_count == 0

    # Two transactions exist: one expense on src, one income on dst, both
    # linked to each other, equal amounts, same date.
    csv_leg = (await db_session.execute(
        select(Transaction).where(
            Transaction.account_id == seed["src_id"],
        )
    )).scalar_one()
    partner_leg = (await db_session.execute(
        select(Transaction).where(
            Transaction.account_id == seed["dst_id"],
        )
    )).scalar_one()
    assert csv_leg.type == TransactionType.EXPENSE
    assert partner_leg.type == TransactionType.INCOME
    assert csv_leg.amount == partner_leg.amount == Decimal("80")
    assert csv_leg.date == partner_leg.date == when
    assert csv_leg.linked_transaction_id == partner_leg.id
    assert partner_leg.linked_transaction_id == csv_leg.id
    # CSV leg flagged is_imported; synthetic partner is not.
    assert csv_leg.is_imported is True
    assert partner_leg.is_imported is False

    linked_calls = [
        c for c in spy.call_args_list
        if c.args and c.args[0] == "transfers.linked"
    ]
    assert len(linked_calls) == 1
    assert linked_calls[0].kwargs["source"] == "import_create_pair"
    assert linked_calls[0].kwargs["recategorized"] is False


async def test_confirm_create_transfer_pair_atomicity_rollback(
    db_session: AsyncSession,
) -> None:
    """If _link_pair raises after both creates, BOTH legs roll back. The DB
    must contain zero transactions afterward.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)

    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="TO SAVINGS", amount=Decimal("80"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="create_transfer_pair",
            partner_account_id=seed["dst_id"],
        )],
    )

    with patch(
        "app.services.import_service._link_pair",
        new=AsyncMock(side_effect=RuntimeError("simulated link failure")),
    ):
        result = await import_service.execute_import(
            db_session, org_id=seed["org_id"], body=body,
        )

    # Surfaces as a row error.
    assert result.paired_count == 0
    assert result.imported_count == 0
    assert result.error_count == 1
    assert "simulated link failure" in result.errors[0].error

    # Zero transactions in the DB — both legs were savepoint-rolled back.
    all_tx = (await db_session.execute(select(Transaction))).scalars().all()
    assert all_tx == []


async def test_confirm_create_transfer_pair_validates_currency_mismatch(
    db_session: AsyncSession,
) -> None:
    """Partner account with a different currency → row error, no rows
    created (savepoint rollback).
    """
    seed = await _seed_two_accounts(db_session)
    # Flip dst's currency to USD (src stays EUR).
    dst = (await db_session.execute(
        select(Account).where(Account.id == seed["dst_id"])
    )).scalar_one()
    dst.currency = "USD"
    await db_session.commit()

    when = datetime.date(2026, 5, 1)
    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="TO SAVINGS", amount=Decimal("50"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="create_transfer_pair",
            partner_account_id=seed["dst_id"],
        )],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )
    assert result.paired_count == 0
    assert result.imported_count == 0
    assert result.error_count == 1
    assert "currency" in result.errors[0].error.lower()

    # No transactions created.
    all_tx = (await db_session.execute(select(Transaction))).scalars().all()
    assert all_tx == []


async def test_confirm_create_transfer_pair_validates_same_account(
    db_session: AsyncSession,
) -> None:
    """partner_account_id == import account → row error, no rows created."""
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="SELF TRANSFER", amount=Decimal("30"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="create_transfer_pair",
            partner_account_id=seed["src_id"],  # same as import account
        )],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )
    assert result.paired_count == 0
    assert result.imported_count == 0
    assert result.error_count == 1
    assert "differ" in result.errors[0].error.lower()
    all_tx = (await db_session.execute(select(Transaction))).scalars().all()
    assert all_tx == []


async def test_confirm_create_transfer_pair_increments_paired_count(
    db_session: AsyncSession,
) -> None:
    """Mixed batch: 2 plain creates + 1 create_transfer_pair → imported=2,
    paired=1. Confirms the new branch contributes to paired_count, not
    imported_count.
    """
    seed = await _seed_two_accounts(db_session)
    when = datetime.date(2026, 5, 1)
    body = ImportConfirmRequest(
        account_id=seed["src_id"],
        default_category_id=seed["transfer_cat_id"],
        rows=[
            ImportConfirmRow(
                row_number=1, date=when,
                description="POS COFFEE A", amount=Decimal("3.50"),
                type="expense",
                category_id=seed["transfer_cat_id"],
                action="create",
            ),
            ImportConfirmRow(
                row_number=2, date=when,
                description="POS COFFEE B", amount=Decimal("4.50"),
                type="expense",
                category_id=seed["transfer_cat_id"],
                action="create",
            ),
            ImportConfirmRow(
                row_number=3, date=when,
                description="TO SAVINGS", amount=Decimal("100"),
                type="expense",
                category_id=seed["transfer_cat_id"],
                action="create_transfer_pair",
                partner_account_id=seed["dst_id"],
            ),
        ],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )
    assert result.imported_count == 2
    assert result.paired_count == 1
    assert result.dropped_duplicate_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 0
    total = (
        result.imported_count + result.paired_count
        + result.dropped_duplicate_count + result.skipped_count
        + result.error_count
    )
    assert total == len(body.rows)


async def test_create_transfer_pair_locks_accounts_in_sorted_order(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Sanity check: account locks for create_transfer_pair are acquired in
    sorted-ID order BEFORE either _create_transaction_no_commit call.
    Mirrors the pattern in convert_and_create_leg's lock-ordering test
    (PR-#118 review fix — deadlock prevention).
    """
    seed = await _seed_two_accounts(db_session)
    # src_id < dst_id by insertion order; verify and capture both for the
    # sorted-ID assertion below.
    src_id = seed["src_id"]
    dst_id = seed["dst_id"]
    assert src_id < dst_id

    real_get_account = import_service.get_account_for_update
    real_create = import_service._create_transaction_no_commit
    call_log: list[tuple[str, int]] = []

    async def spy_get_account(db, account_id, org_id):
        call_log.append(("lock_account", account_id))
        return await real_get_account(db, account_id, org_id)

    async def spy_create(db, org_id, body, *, is_imported=False):
        call_log.append(("create_no_commit", body.account_id))
        return await real_create(db, org_id, body, is_imported=is_imported)

    monkeypatch.setattr(
        import_service, "get_account_for_update", spy_get_account,
    )
    monkeypatch.setattr(
        import_service, "_create_transaction_no_commit", spy_create,
    )

    when = datetime.date(2026, 5, 1)
    body = ImportConfirmRequest(
        account_id=src_id,
        default_category_id=seed["transfer_cat_id"],
        rows=[ImportConfirmRow(
            row_number=1, date=when,
            description="TO SAVINGS", amount=Decimal("80"),
            type="expense",
            category_id=seed["transfer_cat_id"],
            action="create_transfer_pair",
            partner_account_id=dst_id,
            recategorize=False,
        )],
    )

    result = await import_service.execute_import(
        db_session, org_id=seed["org_id"], body=body,
    )
    assert result.paired_count == 1
    assert result.error_count == 0

    # The account-lock calls (entries with op="lock_account") must come
    # BEFORE any "create_no_commit" call, AND in sorted-ID order
    # (lo then hi).
    lock_entries = [c for c in call_log if c[0] == "lock_account"]
    create_entries = [c for c in call_log if c[0] == "create_no_commit"]
    assert len(lock_entries) >= 2, (
        f"Expected >=2 account locks, got: {call_log}"
    )
    assert len(create_entries) >= 2, (
        f"Expected 2 transaction creates, got: {call_log}"
    )

    # First two locks are the sorted account-id pair.
    first_two_lock_ids = [c[1] for c in lock_entries[:2]]
    assert first_two_lock_ids == sorted(first_two_lock_ids), (
        f"Account locks must be sorted-ID order; got {first_two_lock_ids}"
    )
    assert first_two_lock_ids == [src_id, dst_id]

    # Both account-locks happen BEFORE any create_no_commit.
    first_create_idx = next(
        i for i, c in enumerate(call_log) if c[0] == "create_no_commit"
    )
    pre_create_locks = [
        i for i, c in enumerate(call_log[:first_create_idx])
        if c[0] == "lock_account"
    ]
    assert len(pre_create_locks) >= 2, (
        f"Both account locks must precede the first create; got {call_log}"
    )
