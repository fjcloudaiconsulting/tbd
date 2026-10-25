"""Service-layer tests for L3.10 — smart rules / auto-categorization."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry, MerchantDictionaryVote
from app.models.settings import OrgSetting
from app.models.user import Organization
from app.services.category_rules_service import (
    infer_category,
    learn_from_choice,
    normalize_description,
    record_shared_vote,
    share_merchant_data_still_enabled,
    share_merchant_data_value_enabled,
    should_skip_learning,
    withdraw_shared_votes,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # ── Spec-locked cases ────────────────────────────────────────────────
        ("POS PINGO DOCE *1234", "PINGO DOCE"),
        ("LIDL E LEROY MERLIN *4521", "LIDL E LEROY MERLIN"),
        ("AMZN MKTP US*1A2B3C", "AMZN MKTP US"),
        ("SEPA TRANSFER VODAFONE PT 2026-04-15", "VODAFONE PT"),
        # ── Whitespace / casing ──────────────────────────────────────────────
        ("   spotify  AB  ", "SPOTIFY AB"),
        ("##APPLE STORE##", "APPLE STORE"),
        # ── Real-world messy descriptors (architect-requested coverage) ──────
        ("CARD PAYMENT NETFLIX.COM/EUR", "NETFLIX COM EUR"),
        ("PAY 7-ELEVEN STORE", "7 ELEVEN STORE"),
        ("CARTAO LIDL LISBOA *0001", "LIDL LISBOA"),       # PT bank prefix
        ("DEB AMAZON DE BERLIN", "AMAZON DE BERLIN"),       # DEB prefix
        ("SEPA SPOTIFY AB IT60X0542811101000000123456", "SPOTIFY AB"),  # IBAN tail
        ("HTTPS://AMAZON.ES/REF=ABC", "AMAZON ES REF"),     # URL-ish
        ("CONTINENTE LISBOA *4521", "CONTINENTE LISBOA"),
        ("CAFÉ DELTA LISBOA", "CAFE DELTA LISBOA"),         # accent folded (NFKD), not dropped
        ("POS LIDL *1234 *ABCD", "LIDL"),                   # double terminal id
        ("UBER 2026-04-12 2026-04-13", "UBER"),             # double date
        ("MERCADONA 20260412", "MERCADONA"),                # date without dashes
        ("E-LECLERC 24H STATION 042", "E LECLERC 24H STATION 042"),  # 3-digit trailing token kept (brand-suffix safe; see I-1)
        # ── Fallbacks ────────────────────────────────────────────────────────
        ("", ""),                  # empty → empty
        ("X", "X"),                # < 3 chars after cleanup → fallback returns cleaned uppercase
        ("**", ""),                # only noise → empty
        # ── Brand suffix preservation (architect/I-1 sticky-bad-token risk) ───
        ("STORE 24", "STORE 24"),                      # 2-digit brand suffix kept
        ("SUPER 8", "SUPER 8"),                        # 1-digit brand suffix kept
        ("WORTEN 24H STATION", "WORTEN 24H STATION"),  # alphanumeric token kept
        # ── Masked card prefix (architect/I-4) ──────────────────────────────
        ("****0001 STARBUCKS", "STARBUCKS"),
        ("**1234 LIDL LISBOA *9999", "LIDL LISBOA"),   # masked prefix + trailing *id
        # ── Documented trade-offs (low real-world hit rate; not fixing in this PR) ───
        ("PAY DAY LOAN", "DAY LOAN"),                  # I-2: leading "PAY" stripped even when part of name
        ("BRANDIBANXX99ABCDEFGHIJ12345", "BRANDIBAN"), # I-3: glued IBAN-tail IS stripped (regex matches mid-word)
        # ── Trailing company-form suffixes (architect Path B) ────────────────
        ("KPN B.V.", "KPN"),
        ("KPN B V", "KPN"),
        ("KPN BV", "KPN"),
        ("ODIDO NETHERLANDS B.V.", "ODIDO NETHERLANDS"),
        ("FRANK ENERGIE B V", "FRANK ENERGIE"),
        ("AMERICAN EXPRESS EUROPE S.A.", "AMERICAN EXPRESS EUROPE"),
        ("REMOTE B V", "REMOTE"),
        ("ACME GMBH", "ACME"),
        ("WIDGET LTD", "WIDGET"),
        ("FOO INC", "FOO"),
        # Iterative: stacked suffixes
        ("WIDGET B V LTD", "WIDGET"),
        # Trailing-only: leading "B V" preserved (don't strip mid-name)
        ("B V REPAIR SHOP", "B V REPAIR SHOP"),
        # Mid-name preservation
        ("INC POWER STORE", "INC POWER STORE"),
    ],
)
def test_normalize_description(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


def test_normalize_description_handles_none() -> None:
    """raw=None must not crash; returns "" gracefully.

    DB rows can have NULL descriptions; callers shouldn't have to defend.
    """
    assert normalize_description(None) == ""  # type: ignore[arg-type]


def test_should_skip_learning_skips_transfer_via_linked_id() -> None:
    """ORM Transaction with linked_transaction_id set is a transfer leg."""
    tx = SimpleNamespace(linked_transaction_id=42, type="expense")
    assert should_skip_learning(tx) is True


def test_should_skip_learning_keeps_regular_transaction() -> None:
    """No linked-leg link → learn."""
    tx = SimpleNamespace(linked_transaction_id=None, type="expense")
    assert should_skip_learning(tx) is False


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


@pytest_asyncio.fixture
async def seeded_org(db_session: AsyncSession) -> dict:
    """Org with two system categories: groceries + restaurants."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    groceries = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        is_system=True, type=CategoryType.EXPENSE,
    )
    restaurants = Category(
        org_id=org.id, name="Restaurants", slug="restaurants",
        is_system=True, type=CategoryType.EXPENSE,
    )
    db_session.add_all([groceries, restaurants])
    await db_session.commit()
    await db_session.refresh(groceries)
    await db_session.refresh(restaurants)
    return {
        "org_id": org.id,
        "groceries_id": groceries.id,
        "restaurants_id": restaurants.id,
    }


async def test_infer_category_org_rule_wins(db_session: AsyncSession, seeded_org: dict) -> None:
    """An org-local rule beats the shared dictionary even when both match."""
    db_session.add(MerchantDictionaryEntry(
        normalized_token="LIDL", category_slug="groceries", is_seed=True, vote_count=0,
    ))
    db_session.add(CategoryRule(
        org_id=seeded_org["org_id"],
        normalized_token="LIDL",
        raw_description_seen="POS LIDL *0001",
        category_id=seeded_org["restaurants_id"],
        match_count=1,
        source=RuleSource.USER_EDIT,
    ))
    await db_session.commit()

    cat_id, source = await infer_category(
        db_session, org_id=seeded_org["org_id"], description="POS LIDL *9999"
    )
    assert cat_id == seeded_org["restaurants_id"]
    assert source == "org_rule"


async def test_infer_category_falls_through_to_shared(db_session: AsyncSession, seeded_org: dict) -> None:
    db_session.add(MerchantDictionaryEntry(
        normalized_token="PINGO DOCE", category_slug="groceries", is_seed=True, vote_count=0,
    ))
    await db_session.commit()

    cat_id, source = await infer_category(
        db_session, org_id=seeded_org["org_id"], description="POS PINGO DOCE *4521"
    )
    assert cat_id == seeded_org["groceries_id"]
    assert source == "shared_dictionary"


async def test_infer_category_default_when_unknown(db_session: AsyncSession, seeded_org: dict) -> None:
    cat_id, source = await infer_category(
        db_session, org_id=seeded_org["org_id"], description="POS RANDOM SHOP *4521"
    )
    assert cat_id is None
    assert source == "default"


async def test_infer_category_default_when_slug_not_in_org(db_session: AsyncSession, seeded_org: dict) -> None:
    """Dictionary slug doesn't exist as a system category in this org → graceful default."""
    db_session.add(MerchantDictionaryEntry(
        normalized_token="OBSCURE BRAND", category_slug="missing_slug",
        is_seed=True, vote_count=0,
    ))
    await db_session.commit()

    cat_id, source = await infer_category(
        db_session, org_id=seeded_org["org_id"], description="OBSCURE BRAND"
    )
    assert cat_id is None
    assert source == "default"


async def test_learn_from_choice_inserts_new_rule(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    await learn_from_choice(
        db_session,
        org_id=seeded_org["org_id"],
        description="POS LIDL *0001",
        category_id=seeded_org["groceries_id"],
        source="user_pick",
    )
    await db_session.commit()

    rule = (await db_session.execute(
        select(CategoryRule).where(
            CategoryRule.org_id == seeded_org["org_id"],
            CategoryRule.normalized_token == "LIDL",
        )
    )).scalar_one()
    assert rule.category_id == seeded_org["groceries_id"]
    assert rule.match_count == 1
    assert rule.source == RuleSource.USER_PICK
    assert rule.raw_description_seen == "POS LIDL *0001"


async def test_learn_from_choice_upserts_with_new_category(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """Most-recent-wins: a second call with a different category overwrites + bumps count."""
    await learn_from_choice(
        db_session, org_id=seeded_org["org_id"],
        description="POS LIDL *0001", category_id=seeded_org["groceries_id"],
        source="user_pick",
    )
    await db_session.commit()

    await learn_from_choice(
        db_session, org_id=seeded_org["org_id"],
        description="POS LIDL *0002", category_id=seeded_org["restaurants_id"],
        source="user_edit",
    )
    await db_session.commit()

    rule = (await db_session.execute(
        select(CategoryRule).where(CategoryRule.normalized_token == "LIDL")
    )).scalar_one()
    assert rule.category_id == seeded_org["restaurants_id"]
    assert rule.match_count == 2
    assert rule.source == RuleSource.USER_EDIT
    assert rule.raw_description_seen == "POS LIDL *0002"


async def test_learn_from_choice_idempotent_on_same_category(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """Three identical learns → match_count=3, no other rows."""
    for _ in range(3):
        await learn_from_choice(
            db_session, org_id=seeded_org["org_id"],
            description="POS LIDL *0001", category_id=seeded_org["groceries_id"],
            source="user_pick",
        )
        await db_session.commit()

    rules = (await db_session.execute(select(CategoryRule))).scalars().all()
    assert len(rules) == 1
    assert rules[0].match_count == 3
    assert rules[0].category_id == seeded_org["groceries_id"]


async def test_learn_from_choice_empty_token_is_noop(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """Description that normalizes to empty/<3-char fallback that's still empty → don't write."""
    await learn_from_choice(
        db_session, org_id=seeded_org["org_id"],
        description="   ", category_id=seeded_org["groceries_id"],
        source="user_pick",
    )
    await db_session.commit()
    rules = (await db_session.execute(select(CategoryRule))).scalars().all()
    assert rules == []


async def _add_lidl(db: AsyncSession, *, vote_count: int = 0) -> int:
    entry = MerchantDictionaryEntry(
        normalized_token="LIDL", category_slug="groceries", is_seed=True,
        vote_count=vote_count,
    )
    db.add(entry)
    await db.commit()
    return entry.id


async def _second_org(db: AsyncSession) -> dict:
    org = Organization(name="Other Org")
    db.add(org)
    await db.flush()
    groceries = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        is_system=True, type=CategoryType.EXPENSE,
    )
    db.add(groceries)
    await db.commit()
    return {"org_id": org.id, "groceries_id": groceries.id}


async def _vote_state(db: AsyncSession) -> tuple[int, list[tuple[int, int]]]:
    entry = (await db.execute(
        select(MerchantDictionaryEntry).where(
            MerchantDictionaryEntry.normalized_token == "LIDL"
        )
    )).scalar_one()
    await db.refresh(entry)
    votes = (await db.execute(
        select(MerchantDictionaryVote.entry_id, MerchantDictionaryVote.org_id)
        .order_by(MerchantDictionaryVote.org_id)
    )).all()
    return entry.vote_count, [tuple(v) for v in votes]


async def test_record_shared_vote_counts_an_accepted_dictionary_suggestion(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """The server's own suggestion is the dictionary's and the org picked it."""
    entry_id = await _add_lidl(db_session)

    counted = await record_shared_vote(
        db_session, org_id=seeded_org["org_id"],
        description="POS LIDL *9999", category_id=seeded_org["groceries_id"],
    )
    await db_session.commit()

    assert counted is True
    assert await _vote_state(db_session) == (1, [(entry_id, seeded_org["org_id"])])


async def test_record_shared_vote_is_one_per_org(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """Repeated confirms by the same org (same or different descriptor for the
    same token) never add a second vote."""
    entry_id = await _add_lidl(db_session)

    for description in ("POS LIDL *9999", "POS LIDL *9999", "CARTAO LIDL *0001"):
        await record_shared_vote(
            db_session, org_id=seeded_org["org_id"],
            description=description, category_id=seeded_org["groceries_id"],
        )
        await db_session.commit()

    assert await _vote_state(db_session) == (1, [(entry_id, seeded_org["org_id"])])


async def test_record_shared_vote_counts_each_org_once(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    entry_id = await _add_lidl(db_session)
    other = await _second_org(db_session)

    for org in (seeded_org, other, seeded_org, other):
        await record_shared_vote(
            db_session, org_id=org["org_id"],
            description="POS LIDL *9999", category_id=org["groceries_id"],
        )
        await db_session.commit()

    assert await _vote_state(db_session) == (
        2, [(entry_id, seeded_org["org_id"]), (entry_id, other["org_id"])]
    )


async def test_record_shared_vote_adds_to_an_existing_count(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """Counts stored before per-org votes are left as they are; a new vote
    adds one."""
    await _add_lidl(db_session, vote_count=7)

    await record_shared_vote(
        db_session, org_id=seeded_org["org_id"],
        description="POS LIDL *9999", category_id=seeded_org["groceries_id"],
    )
    await db_session.commit()

    count, votes = await _vote_state(db_session)
    assert (count, len(votes)) == (8, 1)


async def test_record_shared_vote_ignores_a_different_category(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """The org overrode the dictionary's suggestion: no vote."""
    await _add_lidl(db_session)

    counted = await record_shared_vote(
        db_session, org_id=seeded_org["org_id"],
        description="POS LIDL *9999", category_id=seeded_org["restaurants_id"],
    )
    await db_session.commit()

    assert counted is False
    assert await _vote_state(db_session) == (0, [])


async def test_record_shared_vote_ignores_an_org_rule_suggestion(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """When the org's own rule wins, the dictionary suggested nothing, even if
    the rule happens to agree with it."""
    await _add_lidl(db_session)
    db_session.add(CategoryRule(
        org_id=seeded_org["org_id"], normalized_token="LIDL",
        raw_description_seen="POS LIDL *1", category_id=seeded_org["groceries_id"],
        match_count=1, source=RuleSource.USER_PICK,
    ))
    await db_session.commit()

    counted = await record_shared_vote(
        db_session, org_id=seeded_org["org_id"],
        description="POS LIDL *9999", category_id=seeded_org["groceries_id"],
    )
    await db_session.commit()

    assert counted is False
    assert await _vote_state(db_session) == (0, [])


async def test_record_shared_vote_noop_when_token_missing(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """Token isn't in the dictionary → silent no-op (we don't auto-promote here)."""
    counted = await record_shared_vote(
        db_session, org_id=seeded_org["org_id"],
        description="POS NOVEL CAFE *0001", category_id=seeded_org["groceries_id"],
    )
    await db_session.commit()
    assert counted is False
    assert (await db_session.execute(select(MerchantDictionaryEntry))).scalars().all() == []
    assert (await db_session.execute(select(MerchantDictionaryVote))).scalars().all() == []


async def test_withdraw_shared_votes_removes_only_that_orgs_votes(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    entry_id = await _add_lidl(db_session)
    other = await _second_org(db_session)
    for org in (seeded_org, other):
        await record_shared_vote(
            db_session, org_id=org["org_id"],
            description="POS LIDL *9999", category_id=org["groceries_id"],
        )
    await db_session.commit()

    removed = await withdraw_shared_votes(db_session, org_id=seeded_org["org_id"])
    await db_session.commit()

    assert removed == 1
    assert await _vote_state(db_session) == (1, [(entry_id, other["org_id"])])

    # Run twice: nothing left to withdraw.
    assert await withdraw_shared_votes(db_session, org_id=seeded_org["org_id"]) == 0


async def test_withdraw_shared_votes_never_takes_a_count_below_zero(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """A vote row on an entry whose stored count is already 0: the count
    stays 0 and the row still goes. Wrong implementation: a plain
    ``vote_count - 1``."""
    entry_id = await _add_lidl(db_session)
    db_session.add(MerchantDictionaryVote(entry_id=entry_id, org_id=seeded_org["org_id"]))
    await db_session.commit()

    assert await withdraw_shared_votes(db_session, org_id=seeded_org["org_id"]) == 1
    await db_session.commit()

    assert await _vote_state(db_session) == (0, [])


@pytest.mark.parametrize("value", ["false", "True", "", None])
def test_only_true_enables_merchant_sharing(value):
    assert share_merchant_data_value_enabled(value) is False
    assert share_merchant_data_value_enabled("true") is True


async def test_merchant_sharing_recheck_reads_the_stored_value(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    org_id = seeded_org["org_id"]
    assert await share_merchant_data_still_enabled(db_session, org_id) is False
    db_session.add(OrgSetting(org_id=org_id, key="share_merchant_data", value="true"))
    await db_session.commit()
    assert await share_merchant_data_still_enabled(db_session, org_id) is True


async def test_sharing_recheck_and_vote_withdrawal_use_ordered_locking_reads(
    db_session: AsyncSession, seeded_org: dict
) -> None:
    """The reads that make an opt-out and an import's votes exclusive must be
    locking reads on MySQL, and the withdrawal updates entries in ``entry_id``
    order so two withdrawals lock them in the same order. SQLite drops
    FOR UPDATE, so the rendered SQL is checked; the MySQL interleavings are
    in test_merchant_vote_sharing_mysql.py."""
    from sqlalchemy.dialects import mysql

    org_id = seeded_org["org_id"]
    for token in ("LIDL", "ALDI"):
        db_session.add(MerchantDictionaryEntry(
            normalized_token=token, category_slug="groceries", is_seed=True, vote_count=1,
        ))
    await db_session.flush()
    ids = (await db_session.execute(
        select(MerchantDictionaryEntry.id).order_by(MerchantDictionaryEntry.id.desc())
    )).scalars().all()
    # Inserted highest entry id first, so the vote table's own order is not
    # the entry order.
    db_session.add_all([MerchantDictionaryVote(entry_id=i, org_id=org_id) for i in ids])
    await db_session.commit()

    seen: list = []
    real = db_session.execute

    async def _spy(stmt, *args, **kwargs):
        seen.append(stmt)
        return await real(stmt, *args, **kwargs)

    db_session.execute = _spy  # type: ignore[assignment]
    try:
        await share_merchant_data_still_enabled(db_session, org_id)
        await withdraw_shared_votes(db_session, org_id=org_id)
    finally:
        db_session.execute = real  # type: ignore[assignment]
    rendered = [str(s.compile(dialect=mysql.dialect())) for s in seen]
    assert rendered[0].rstrip().endswith("FOR UPDATE") and "FROM org_settings" in rendered[0]
    vote_read = rendered[1]
    assert "FROM merchant_dictionary_votes" in vote_read
    assert vote_read.rstrip().endswith("FOR UPDATE")
    # Sorted in Python: ORDER BY entry_id would let MySQL lock other orgs'
    # vote rows through the (entry_id, org_id) index.
    assert "ORDER BY" not in vote_read
    updated = [s.compile().params["id_1"] for s in seen[2:]
               if str(s).startswith("UPDATE merchant_dictionary ")]
    assert updated == sorted(ids)
