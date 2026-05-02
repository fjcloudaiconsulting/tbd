"""Service-layer tests for L3.10 — smart rules / auto-categorization."""
import pytest

from app.services.category_rules_service import normalize_description


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
        ("CAFÉ DELTA LISBOA", "CAF DELTA LISBOA"),          # accent stripped
        ("POS LIDL *1234 *ABCD", "LIDL"),                   # double terminal id
        ("UBER 2026-04-12 2026-04-13", "UBER"),             # double date
        ("MERCADONA 20260412", "MERCADONA"),                # date without dashes
        ("E-LECLERC 24H STATION 042", "E LECLERC 24H STATION"),  # brand-internal digits
        # ── Fallbacks ────────────────────────────────────────────────────────
        ("", ""),                  # empty → empty
        ("X", "X"),                # < 3 chars after cleanup → fallback returns cleaned uppercase
        ("**", ""),                # only noise → empty
    ],
)
def test_normalize_description(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected
