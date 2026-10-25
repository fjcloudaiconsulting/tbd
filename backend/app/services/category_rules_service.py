"""Smart rules / auto-categorization service (L3.10).

Deterministic rule-based suggestion + learning. No AI in this pass.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal, Optional

from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry, MerchantDictionaryVote
from app.models.settings import OrgSetting
from app.services.transaction_filters import is_transfer_leg

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

# Trailing company-form suffixes. Stripped iteratively from the END of the
# token sequence after punctuation collapse, so "B.V.", "B V", and "BV"
# all behave consistently. Architect-cautioned: trailing-only — never strip
# these in the middle of a merchant name.
_TRAILING_COMPANY_FORMS: tuple[tuple[str, ...], ...] = (
    ("B", "V"),     # NL: B.V. / B V
    ("N", "V"),     # NL: N.V. / N V
    ("S", "A"),     # PT/ES/IT: S.A. / S A
    ("BV",),
    ("NV",),
    ("SA",),
    ("BVBA",),      # BE: B.V.B.A.
    ("GMBH",),      # DE
    ("AG",),        # CH/DE: Aktiengesellschaft
    ("INC",),
    ("LLC",),
    ("LTD",),
)


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
      7. Strip trailing company-form suffixes (B.V. / N.V. / S.A. / BV / GMBH / INC / LLC / LTD ...).
      8. Fallback: if cleanup yielded < 3 chars, return cleaned-uppercase original.
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

    # Step 7 — strip trailing company-form suffixes (iteratively, so "FOO B V"
    # and "FOO INC LLC" both collapse to "FOO"). TRAILING-ONLY by construction:
    # we only inspect ``tokens[-len(form):]``, never mid-sequence.
    while tokens:
        stripped = False
        for form in _TRAILING_COMPANY_FORMS:
            n = len(form)
            if len(tokens) >= n and tuple(tokens[-n:]) == form:
                tokens = tokens[:-n]
                stripped = True
                break
        if not stripped:
            break

    candidate = " ".join(tokens).strip()
    if len(candidate) < 3:
        return _fallback(raw)
    return candidate


def should_skip_learning(obj) -> bool:
    """True for any object representing a transfer leg.

    Now that ImportConfirmRow no longer carries an is_transfer field (PR-C),
    transfer-leg detection is single-source: the ORM Transaction's
    ``linked_transaction_id``, accessed via ``is_transfer_leg``. The
    ``hasattr`` guard preserves duck-typing safety: any non-Transaction object
    passed through (e.g. a bare Pydantic row in legacy callers) returns False
    rather than raising AttributeError.

    Why: transfers don't have a meaningful merchant — they're inter-account
    movement. Learning a rule from a transfer would map "TRANSFER TO SAVINGS"
    to whatever category the user picked for accounting purposes, which would
    then mis-categorize legitimate same-merchant payments later.
    """
    return is_transfer_leg(obj) if hasattr(obj, "linked_transaction_id") else False


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


SHARE_MERCHANT_DATA_KEY = "share_merchant_data"


def share_merchant_data_value_enabled(value: Optional[str]) -> bool:
    """The one reading of a stored ``share_merchant_data`` value: only ``"true"``."""
    return value == "true"


async def share_merchant_data_still_enabled(db: AsyncSession, org_id: int) -> bool:
    """Re-read ``share_merchant_data`` under a row lock before voting (BE-28).

    Turning sharing off updates (or deletes) that row and withdraws the
    org's votes in one transaction (``withdraw_shared_votes``). The lock
    makes the two exclusive, as BE-18 does for tags: an import either waits
    for the opt-out to commit and then sees it, or commits first and its
    votes are then withdrawn by it (the withdrawal reads vote rows with a
    locking read too).
    """
    row = await db.execute(
        select(OrgSetting.value)
        .where(OrgSetting.org_id == org_id, OrgSetting.key == SHARE_MERCHANT_DATA_KEY)
        .with_for_update()
    )
    return share_merchant_data_value_enabled(row.scalar_one_or_none())


async def record_shared_vote(
    db: AsyncSession,
    *,
    org_id: int,
    description: str,
    category_id: int,
) -> bool:
    """Count ``org_id``'s vote for a shared-dictionary entry (BE-28).

    The vote is decided here, never from the client's suggestion fields:
    the server recomputes its own suggestion for ``description``
    (``infer_category``) and the org votes only when that suggestion came
    from the shared dictionary and ``category_id`` is the category it
    resolved to. Call it BEFORE ``learn_from_choice`` for the same row,
    since the rule that call writes would make the org's own rule the
    suggestion.

    One vote per org per entry: a ``MerchantDictionaryVote`` row is inserted
    under ``UNIQUE(entry_id, org_id)`` and ``vote_count`` goes up by one only
    when that insert creates a new row, so repeating a confirm is a no-op.
    The insert runs in a SAVEPOINT: a concurrent confirm by the same org
    waits on the unique key and then fails it, which rolls back only the
    savepoint. Returns True iff a new vote was counted. Caller commits.
    """
    suggested_id, source = await infer_category(
        db, org_id=org_id, description=description
    )
    if source != "shared_dictionary" or suggested_id != category_id:
        return False

    entry_id = (await db.execute(
        select(MerchantDictionaryEntry.id).where(
            MerchantDictionaryEntry.normalized_token
            == normalize_description(description)
        )
    )).scalar_one_or_none()
    if entry_id is None:
        return False

    existing = (await db.execute(
        select(MerchantDictionaryVote.id).where(
            MerchantDictionaryVote.entry_id == entry_id,
            MerchantDictionaryVote.org_id == org_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return False
    try:
        async with db.begin_nested():
            db.add(MerchantDictionaryVote(entry_id=entry_id, org_id=org_id))
            await db.flush()
    except IntegrityError:
        # Counted already by a concurrent confirm of the same org.
        return False

    # ``vote_count + 1`` in SQL so concurrent votes from different orgs
    # don't lose increments under repeatable-read isolation.
    await db.execute(
        update(MerchantDictionaryEntry)
        .where(MerchantDictionaryEntry.id == entry_id)
        .values(vote_count=MerchantDictionaryEntry.vote_count + 1)
    )
    return True


async def withdraw_shared_votes(db: AsyncSession, *, org_id: int) -> int:
    """Remove every shared-dictionary vote of ``org_id`` (sharing turned off,
    org reset or deleted).

    Each entry loses the one count the org's vote added, then the vote rows
    go, so ``vote_count`` keeps its legacy baseline plus one per remaining
    vote row. The decrement stops at 0. The rows are read with a locking
    read, so a vote committed while this transaction waited for the org's
    ``share_merchant_data`` row is withdrawn too. Entries are updated in
    ``entry_id`` order, so two withdrawals lock them in the same order. The
    sort is done here, not in SQL: an ``ORDER BY entry_id`` lets MySQL read
    through the ``(entry_id, org_id)`` unique index and lock other orgs'
    vote rows, which deadlocks concurrent withdrawals. Staged on the
    caller's session; the caller commits. Returns the number of votes
    removed.
    """
    rows = (await db.execute(
        select(MerchantDictionaryVote.id, MerchantDictionaryVote.entry_id)
        .where(MerchantDictionaryVote.org_id == org_id)
        .with_for_update()
    )).all()
    if not rows:
        return 0
    for _, entry_id in sorted(rows, key=lambda row: row.entry_id):
        await db.execute(
            update(MerchantDictionaryEntry)
            .where(MerchantDictionaryEntry.id == entry_id)
            .values(vote_count=case(
                (MerchantDictionaryEntry.vote_count > 0,
                 MerchantDictionaryEntry.vote_count - 1),
                else_=0,
            ))
        )
    await db.execute(
        delete(MerchantDictionaryVote).where(
            MerchantDictionaryVote.id.in_([vote_id for vote_id, _ in rows])
        )
    )
    return len(rows)
