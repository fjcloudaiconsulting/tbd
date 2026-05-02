"""Smart rules / auto-categorization service (L3.10).

Deterministic rule-based suggestion + learning. No AI in this pass.
"""
from __future__ import annotations

import re
import unicodedata

# URL scheme prefix (HTTP / HTTPS) — stripped before bank-noise so `HTTPS://AMAZON…` collapses cleanly.
_URL_SCHEME = re.compile(r"^\s*HTTPS?://", re.IGNORECASE)

# Bank-noise that appears at the front of the descriptor.
_LEADING_NOISE = re.compile(
    r"^\s*(POS|CARD\s*PAYMENT|CARD|PAY|SEPA(?:\s+TRANSFER)?|DEB|CARTAO)\s+",
    re.IGNORECASE,
)

# Tail markers we strip iteratively (multiple may stack).
# Order matters: IBAN before terminal-id, because IBAN starts with letters and
# the terminal-id pattern would eat its leading two letters.
_TRAILING_TOKENS = re.compile(
    r"(?:"
    r"\s*[A-Z]{2}\d{2}[A-Z0-9]{10,30}|"   # IBAN: 2 letters + 2 digits + 10-30 alnum
    r"\s*\*[A-Z0-9]+|"                    # *1A2B, *0001
    r"\s+\d{4}-\d{2}-\d{2}|"              # 2026-04-15
    r"\s+\d{8}|"                          # 20260412 (only when space-separated, never mid-word)
    r"=[A-Z0-9]+"                         # =ABC URL query-value tail
    r")\s*$",
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_MULTI_SPACE = re.compile(r"\s+")

_LEADING_STOPWORDS = {"TRANSFER", "PAYMENT", "DEBIT", "CREDIT", "TXN", "TX"}


def _strip_accents(s: str) -> str:
    """ASCII-strict drop of non-ASCII codepoints.

    Note: this DROPS non-ASCII characters rather than folding them
    (CAFÉ → CAF, not CAFE). The spec's accent-test case asserts the drop
    behavior — folding `É`→`E` and keeping it bleeds noise into rules
    on mixed-locale descriptors, so we drop instead.
    """
    return s.encode("ascii", "ignore").decode("ascii")


def _fallback(raw: str) -> str:
    """Cleaned uppercase original, alphanumerics-and-spaces only."""
    base = _NON_ALNUM.sub(" ", _strip_accents(raw).strip().upper())
    return _MULTI_SPACE.sub(" ", base).strip()


def normalize_description(raw: str) -> str:
    """Bank descriptor → canonical uppercase merchant token.

    Pipeline:
      1. Strip whitespace, drop non-ASCII chars, uppercase.
      2. Strip leading URL scheme (HTTPS:// / HTTP://) then bank-noise (POS / CARD / PAY / SEPA / DEB / CARTAO).
      3. Iteratively strip trailing dates / terminal IDs / IBANs / URL query-values
         (loop because two-in-a-row is common: e.g. `... *1234 *ABCD`).
      4. Replace non-alphanumeric runs with a single space; collapse runs.
      5. Drop a residual leading stopword (e.g. `TRANSFER` left over after `SEPA TRANSFER ...`).
      6. Drop trailing pure-digit tokens (date residue, terminal IDs the regex missed).
      7. Fallback: if cleanup yielded < 3 chars, return cleaned-uppercase original.
    """
    if not raw:
        return ""
    s = _strip_accents(raw).strip().upper()
    if not s:
        return ""

    # Step 2 — URL scheme then bank-noise (one pass each; descriptors don't stack these).
    s = _URL_SCHEME.sub("", s, count=1)
    s = _LEADING_NOISE.sub("", s, count=1)

    # Step 3 — iterate trailing markers until stable.
    while True:
        new = _TRAILING_TOKENS.sub("", s).rstrip()
        if new == s:
            break
        s = new

    # Step 4 — collapse non-alnum.
    s = _NON_ALNUM.sub(" ", s)
    s = _MULTI_SPACE.sub(" ", s).strip()
    if not s:
        return _fallback(raw)

    tokens = s.split()

    # Step 5 — drop residual leading stopword.
    if tokens and tokens[0] in _LEADING_STOPWORDS:
        tokens = tokens[1:]

    # Step 6 — strip trailing pure-digit tokens.
    while tokens and tokens[-1].isdigit():
        tokens.pop()

    candidate = " ".join(tokens).strip()
    if len(candidate) < 3:
        return _fallback(raw)
    return candidate
