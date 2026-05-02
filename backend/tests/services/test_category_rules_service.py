"""Service-layer tests for L3.10 — smart rules / auto-categorization."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry
from app.models.user import Organization
from app.services.category_rules_service import (
    infer_category,
    normalize_description,
    should_skip_learning,
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


def test_should_skip_learning_skips_preview_row_marked_transfer() -> None:
    """ImportConfirmRow with is_transfer=True must skip."""
    row = SimpleNamespace(linked_transaction_id=None, is_transfer=True)
    assert should_skip_learning(row) is True


def test_should_skip_learning_keeps_regular_transaction() -> None:
    """Neither linked nor flagged → learn."""
    tx = SimpleNamespace(linked_transaction_id=None, type="expense", is_transfer=False)
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
