"""Smart rules / auto-categorization service (L3.10).

Deterministic rule-based suggestion + learning. No AI in this pass.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry

# URL scheme prefix (HTTP / HTTPS) — stripped before bank-noise so `HTTPS://AMAZON…` collapses cleanly.
_URL_SCHEME = re.compile(r"^\s*HTTPS?://", re.IGNORECASE)

# Masked card numbers appearing at the front: "****0001", "*1234", "**5678 ".
_LEADING_MASKED_CARD = re.compile(r"^\s*\*+\s*\d{2,}\s+", re.IGNORECASE)

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
    """NFKD decompose + drop combining marks → fold accents to ASCII letters.

    Why fold and not drop: bank descriptors for the same merchant vary in encoding
    (e.g. `CAFÉ DELTA` in one bank, `CAFE DELTA` in another). Folding maps them
    to the same token; dropping would fracture coverage. Architectural decision
    captured in project_architect_review_2026_05_02.md.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


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

    # Step 2a — strip leading URL scheme if present.
    s = _URL_SCHEME.sub("", s, count=1)
    # Step 2b — strip leading masked-card prefix (e.g. "****0001 ").
    s = _LEADING_MASKED_CARD.sub("", s, count=1)
    # Step 2c — strip leading bank-noise (POS / CARD / PAY / SEPA / DEB / CARTAO).
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

    # Step 6 — strip trailing pure-digit tokens that look like date/terminal-id residue
    # (4+ digits). Keep 1-3 digit tokens because they're often brand suffixes
    # like "STORE 24" or "SUPER 8". Architect note (sticky-bad-token risk):
    # collapsing "STORE 24" and "STORE" into one token would merge two
    # different merchants in the rules dictionary.
    while tokens and tokens[-1].isdigit() and len(tokens[-1]) >= 4:
        tokens.pop()

    candidate = " ".join(tokens).strip()
    if len(candidate) < 3:
        return _fallback(raw)
    return candidate


def should_skip_learning(obj) -> bool:
    """True for any object representing a transfer.

    Duck-typed:
      - ORM Transaction → uses ``linked_transaction_id`` (non-None means it's
        paired with another leg, i.e. a transfer).
      - ImportPreviewRow / ImportConfirmRow → uses ``is_transfer``.

    Either attribute being truthy short-circuits to True. Objects with neither
    attribute return False.

    Why: transfers don't have a meaningful merchant — they're inter-account
    movement. Learning a rule from a transfer would map "TRANSFER TO SAVINGS"
    to whatever category the user picked for accounting purposes, which would
    then mis-categorize legitimate same-merchant payments later.
    """
    if getattr(obj, "linked_transaction_id", None) is not None:
        return True
    if getattr(obj, "is_transfer", False):
        return True
    return False


InferSource = Literal["org_rule", "shared_dictionary", "default"]


async def infer_category(
    db: AsyncSession, *, org_id: int, description: str
) -> tuple[int | None, InferSource]:
    """Resolve (category_id, source) for a transaction description.

    Lookup order (architect-locked):
      1. Org-local ``category_rules`` keyed by (org_id, normalized_token).
      2. Shared ``merchant_dictionary`` token → resolve ``category_slug``
         against this org's ``categories`` (is_system=True, slug=...).
      3. Default — return (None, "default").

    A future LLM tier (LAI.1) will slot between (2) and (3) once we have
    rule-coverage data — not in this PR.
    """
    token = normalize_description(description)
    if not token:
        return None, "default"

    # Tier 1 — org rule
    result = await db.execute(
        select(CategoryRule.category_id).where(
            CategoryRule.org_id == org_id,
            CategoryRule.normalized_token == token,
        )
    )
    cat_id = result.scalar_one_or_none()
    if cat_id is not None:
        return cat_id, "org_rule"

    # Tier 2 — shared dictionary → resolve slug → org's category id
    result = await db.execute(
        select(MerchantDictionaryEntry.category_slug).where(
            MerchantDictionaryEntry.normalized_token == token
        )
    )
    slug = result.scalar_one_or_none()
    if slug:
        result = await db.execute(
            select(Category.id).where(
                Category.org_id == org_id,
                Category.slug == slug,
                Category.is_system.is_(True),
            )
        )
        org_cat_id = result.scalar_one_or_none()
        if org_cat_id is not None:
            return org_cat_id, "shared_dictionary"

    # Tier 3 — default
    return None, "default"


async def learn_from_choice(
    db: AsyncSession,
    *,
    org_id: int,
    description: str,
    category_id: int,
    source: str,
) -> None:
    """Upsert into category_rules. Caller controls the transaction (no commit here).

    Behaviour (architect-locked, do NOT add conservative-overwrite logic):
      - First write for (org_id, normalized_token): insert with match_count=1.
      - Subsequent writes: overwrite category_id, source, and raw_description_seen.
        Increment match_count. Most-recent-wins.
      - Empty normalized token: no-op.

    Why most-recent-wins: rules come from EXPLICIT user picks/edits, not
    probabilistic AI inference. When the user says "Spotify is Entertainment,"
    we obey, even if a previous high-match-count rule said otherwise. The
    metric scaffold (smart_rules.import_executed) lets us measure whether
    sticky-bad-rule churn is a real problem; if so, we add policy then.
    """
    token = normalize_description(description)
    if not token:
        return

    result = await db.execute(
        select(CategoryRule).where(
            CategoryRule.org_id == org_id,
            CategoryRule.normalized_token == token,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        db.add(CategoryRule(
            org_id=org_id,
            normalized_token=token,
            raw_description_seen=description[:255],
            category_id=category_id,
            match_count=1,
            source=RuleSource(source),
        ))
        return

    rule.category_id = category_id
    rule.source = RuleSource(source)
    rule.raw_description_seen = description[:255]
    rule.match_count = (rule.match_count or 0) + 1


async def bump_shared_vote(db: AsyncSession, *, description: str) -> None:
    """Atomically increment vote_count on the matching shared-dictionary entry.

    No-op if the normalized token isn't in the dictionary — promotion of
    new tokens is a future feature. Caller controls the transaction.

    Uses ``UPDATE ... SET vote_count = vote_count + 1`` so concurrent imports
    from different orgs don't lose increments under repeatable-read isolation
    (the previous read-modify-write pattern would clobber peers under load).
    """
    token = normalize_description(description)
    if not token:
        return
    await db.execute(
        update(MerchantDictionaryEntry)
        .where(MerchantDictionaryEntry.normalized_token == token)
        .values(vote_count=MerchantDictionaryEntry.vote_count + 1)
    )
