"""Service-layer tests for L3.10 — smart rules / auto-categorization."""
import pytest

from types import SimpleNamespace

from app.services.category_rules_service import (
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
