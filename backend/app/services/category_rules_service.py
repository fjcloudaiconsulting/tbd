"""Smart rules / auto-categorization service (L3.10).

Deterministic rule-based suggestion + learning. No AI in this pass.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry
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


def _collation_key(value: str) -> str:
    """Fold a stored token or slug the way the default MySQL collation compares.

    ``utf8mb4_0900_ai_ci`` ignores case and accents, so the per-row
    ``infer_category`` query matches a stored row that differs from the query
    token only in case or accents. The batch variant matches in Python, so it
    folds both sides the same way. Query tokens are already uppercase ASCII
    (``normalize_description`` output), so for them the fold is the identity.
    """
    return _strip_accents(value).upper()


def _pick(rows: list, token: str, attr: str):
    """The row the per-row lookup would return for ``token``, or ``None``.

    An exact match wins; otherwise the collation-equal rows decide. More than
    one candidate is what ``scalar_one_or_none`` raises on, so it raises here
    too.
    """
    exact = [r for r in rows if getattr(r, attr) == token]
    candidates = exact or rows
    if len(candidates) > 1:
        raise MultipleResultsFound(
            "Multiple rows were found when one or none was required"
        )
    return candidates[0] if candidates else None


async def infer_categories(
    db: AsyncSession, *, org_id: int, descriptions: Iterable[str]
) -> dict[str, tuple[int | None, InferSource]]:
    """``infer_category`` for many descriptions in a fixed number of queries.

    TBD-736: the import preview called ``infer_category`` once per row (one to
    three queries each). This resolves every distinct description with at most
    three set-based queries (org rules, shared dictionary, then the org's
    system categories for the dictionary slugs) and returns the same
    ``(category_id, source)`` the per-row call returns, keyed by description.
    """
    tokens = {d: normalize_description(d) for d in set(descriptions)}
    wanted = {t for t in tokens.values() if t}
    resolved: dict[str, tuple[int | None, InferSource]] = {}

    # Tier 1 — org rules for every token in one query.
    rules_by_key: dict[str, list] = {}
    if wanted:
        rule_rows = (await db.execute(
            select(CategoryRule.normalized_token, CategoryRule.category_id).where(
                CategoryRule.org_id == org_id,
                CategoryRule.normalized_token.in_(wanted),
            )
        )).all()
        for r in rule_rows:
            rules_by_key.setdefault(_collation_key(r.normalized_token), []).append(r)
    for token in wanted:
        rule = _pick(rules_by_key.get(token, []), token, "normalized_token")
        if rule is not None and rule.category_id is not None:
            resolved[token] = (rule.category_id, "org_rule")

    # Tier 2 — shared dictionary for the tokens tier 1 left open, then the
    # org's system categories for the slugs it names.
    open_tokens = wanted - resolved.keys()
    dict_by_key: dict[str, list] = {}
    if open_tokens:
        dict_rows = (await db.execute(
            select(
                MerchantDictionaryEntry.normalized_token,
                MerchantDictionaryEntry.category_slug,
            ).where(MerchantDictionaryEntry.normalized_token.in_(open_tokens))
        )).all()
        for r in dict_rows:
            dict_by_key.setdefault(_collation_key(r.normalized_token), []).append(r)
    slug_for_token: dict[str, str] = {}
    for token in open_tokens:
        entry = _pick(dict_by_key.get(token, []), token, "normalized_token")
        if entry is not None and entry.category_slug:
            slug_for_token[token] = entry.category_slug

    cats_by_key: dict[str, list] = {}
    if slug_for_token:
        cat_rows = (await db.execute(
            select(Category.slug, Category.id).where(
                Category.org_id == org_id,
                Category.slug.in_(set(slug_for_token.values())),
                Category.is_system.is_(True),
            )
        )).all()
        for r in cat_rows:
            cats_by_key.setdefault(_collation_key(r.slug), []).append(r)
    for token, slug in slug_for_token.items():
        cat = _pick(cats_by_key.get(_collation_key(slug), []), slug, "slug")
        if cat is not None and cat.id is not None:
            resolved[token] = (cat.id, "shared_dictionary")

    # Tier 3 — default.
    return {
        d: resolved.get(t, (None, "default")) if t else (None, "default")
        for d, t in tokens.items()
    }


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
