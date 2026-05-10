"""Tag service (PR-Tags-A).

All tag mutations and reads route through this module. The router is a
thin delegate (matches the pattern in ``transactions.py`` /
``categories.py``).

Three responsibilities:

1. **Org-local tags**: create / rename / delete / list with usage
   counts, normalize names consistently before the unique check.
2. **Transaction tag set**: replace the join rows for a transaction
   atomically with a cap of ``MAX_TAGS_PER_TRANSACTION``.
3. **Cross-org dictionary**: write side (``record_dictionary_contribution``)
   gated on ``share_tag_data=true``; read side
   (``suggest_tags`` three-pass query) honoring the k-anonymity floor.

The dictionary write side uses a separate ``tag_dictionary_contributors``
row insert with a unique constraint to enforce "one contribution per
(org, tag) name" and increments the denormalized
``tag_dictionary.contributor_org_count`` only when that insert created a
new row. Both mutations run in the same transaction as the org-local
tag create so the count cannot drift from
``COUNT(DISTINCT contributor_org_id)``.
"""
from __future__ import annotations

import re
from typing import Iterable, Literal, Optional

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.settings import OrgSetting
from app.models.tag import (
    Tag,
    TagDictionary,
    TagDictionaryContributor,
    TransactionTag,
)
from app.models.transaction import Transaction
from app.schemas.tag import (
    MAX_TAGS_PER_TRANSACTION,
    SHARED_DICTIONARY_MIN_CONTRIBUTORS,
    TAG_NAME_MAX_LENGTH,
    SuggestionSource,
    TagSuggestion,
)
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Allowed character class on the normalized form: lowercase ASCII letters,
# digits, single space, and hyphen. Anything else triggers a typed
# ValidationError so users see "tag contains an invalid character" rather
# than silent stripping.
_ALLOWED_CHARS_RE = re.compile(r"^[a-z0-9 \-]+$")
_WHITESPACE_RUN_RE = re.compile(r"\s+")

# Cross-org promotion guards (spec section 3.2). Tags longer than this
# threshold OR with more than one hyphen group are excluded from
# dictionary contribution entirely.
_DICTIONARY_NAME_MAX_LENGTH = 16
_DICTIONARY_MAX_HYPHEN_GROUPS = 1


def normalize_tag_name(raw: str) -> str:
    """Canonical form for storage and uniqueness.

    1. Strip surrounding whitespace.
    2. Lowercase.
    3. Collapse internal whitespace runs to a single space.
    4. Reject empty strings, strings > 32 chars, and strings containing
       characters outside ``[a-z0-9 \\-]``.
    """
    if raw is None:
        raise ValidationError("Tag name is required")
    cleaned = _WHITESPACE_RUN_RE.sub(" ", raw.strip().lower())
    if not cleaned:
        raise ValidationError("Tag name is required")
    if len(cleaned) > TAG_NAME_MAX_LENGTH:
        raise ValidationError(
            f"Tag name must be {TAG_NAME_MAX_LENGTH} characters or fewer"
        )
    if not _ALLOWED_CHARS_RE.match(cleaned):
        raise ValidationError(
            "Tag name may only contain lowercase letters, digits, "
            "spaces, and hyphens"
        )
    return cleaned


def _is_dictionary_eligible(name_normalized: str) -> bool:
    """Length / hyphen-group filter for cross-org promotion.

    Returns True only when the tag is short enough and structurally
    generic enough to be a candidate for dictionary contribution. The
    actual gate (org opt-in) is checked separately by the caller.
    """
    if len(name_normalized) > _DICTIONARY_NAME_MAX_LENGTH:
        return False
    hyphen_groups = name_normalized.count("-")
    if hyphen_groups > _DICTIONARY_MAX_HYPHEN_GROUPS:
        return False
    return True


# ---------------------------------------------------------------------------
# Org-settings helpers
# ---------------------------------------------------------------------------

_SHARE_TAG_DATA_KEY = "share_tag_data"


async def is_share_tag_data_enabled(db: AsyncSession, org_id: int) -> bool:
    """Read the per-org ``share_tag_data`` toggle. Default false.

    The OrgSetting KV pattern stores ``"true" | "false"`` as a string;
    we coerce here so the rest of the service can branch on a real bool.
    """
    row = await db.execute(
        select(OrgSetting.value).where(
            OrgSetting.org_id == org_id,
            OrgSetting.key == _SHARE_TAG_DATA_KEY,
        )
    )
    val = row.scalar_one_or_none()
    return val == "true"


# ---------------------------------------------------------------------------
# Per-org tag CRUD
# ---------------------------------------------------------------------------


async def list_org_tags(
    db: AsyncSession,
    org_id: int,
    *,
    q: Optional[str] = None,
    include_usage: bool = True,
) -> list[tuple[Tag, int]]:
    """Return ``[(Tag, usage_count), ...]`` for an org.

    ``q`` is a substring match against ``name_normalized`` (case
    insensitive because the column is already lowercase).
    """
    base = select(
        Tag,
        func.count(TransactionTag.tag_id).label("usage_count"),
    ).outerjoin(TransactionTag, TransactionTag.tag_id == Tag.id).where(
        Tag.org_id == org_id
    )
    if q:
        q_norm = q.strip().lower()
        if q_norm:
            base = base.where(Tag.name_normalized.like(f"%{q_norm}%"))
    base = base.group_by(Tag.id).order_by(
        func.count(TransactionTag.tag_id).desc(),
        Tag.name_normalized.asc(),
    )
    result = await db.execute(base)
    rows = result.all()
    return [(tag, int(count)) for tag, count in rows]


async def _get_tag_by_normalized(
    db: AsyncSession, org_id: int, name_normalized: str
) -> Optional[Tag]:
    row = await db.execute(
        select(Tag).where(
            Tag.org_id == org_id,
            Tag.name_normalized == name_normalized,
        )
    )
    return row.scalar_one_or_none()


async def create_tag(
    db: AsyncSession,
    *,
    org_id: int,
    name: str,
    created_by_user_id: Optional[int],
) -> Tag:
    """Create a new tag for the org, normalizing first.

    Raises ``ConflictError`` if an existing tag in this org has the same
    normalized form. Triggers the dictionary contribution path when the
    org has ``share_tag_data=true`` and the tag passes the eligibility
    filter, both inside the same transaction.
    """
    name_normalized = normalize_tag_name(name)
    display_name = _WHITESPACE_RUN_RE.sub(" ", name.strip())
    existing = await _get_tag_by_normalized(db, org_id, name_normalized)
    if existing is not None:
        raise ConflictError(f"Tag '{name_normalized}' already exists")
    tag = Tag(
        org_id=org_id,
        name=display_name,
        name_normalized=name_normalized,
        created_by_user_id=created_by_user_id,
    )
    db.add(tag)
    # Flush so we have the id available if the caller needs it; the
    # commit is the router's responsibility.
    await db.flush()

    # Dictionary contribution side. Failures here raise a typed
    # exception which rolls back the whole create, that's the right
    # behaviour because the caller asked for an atomic "create + maybe
    # contribute" operation.
    if await is_share_tag_data_enabled(db, org_id):
        await _record_dictionary_contribution(
            db, org_id=org_id, name_normalized=name_normalized
        )
    return tag


async def rename_tag(
    db: AsyncSession,
    *,
    org_id: int,
    tag_id: int,
    new_name: str,
) -> Tag:
    """Rename a tag in place, refreshing ``name_normalized``.

    No dictionary contribution is triggered on rename (the new name was
    not "first seen" in the dictionary sense, it could equally be a
    typo correction; we don't want to inflate dictionary counts on
    typo).
    """
    new_name_normalized = normalize_tag_name(new_name)
    new_display = _WHITESPACE_RUN_RE.sub(" ", new_name.strip())
    row = await db.execute(
        select(Tag).where(Tag.id == tag_id, Tag.org_id == org_id)
    )
    tag = row.scalar_one_or_none()
    if tag is None:
        raise NotFoundError("tag")
    if tag.name_normalized == new_name_normalized:
        # No-op rename (only display casing or whitespace changed).
        tag.name = new_display
        return tag
    collision = await _get_tag_by_normalized(db, org_id, new_name_normalized)
    if collision is not None and collision.id != tag.id:
        raise ConflictError(f"Tag '{new_name_normalized}' already exists")
    tag.name = new_display
    tag.name_normalized = new_name_normalized
    return tag


async def delete_tag(
    db: AsyncSession,
    *,
    org_id: int,
    tag_id: int,
) -> Tag:
    """Delete a tag. Cascade detaches it from every transaction via the
    DB-side ``ON DELETE CASCADE`` on ``transaction_tags``.

    Returns the deleted tag (still in-memory so the caller can include
    its name in audit details).
    """
    row = await db.execute(
        select(Tag).where(Tag.id == tag_id, Tag.org_id == org_id)
    )
    tag = row.scalar_one_or_none()
    if tag is None:
        raise NotFoundError("tag")
    await db.delete(tag)
    return tag


# ---------------------------------------------------------------------------
# Transaction tag set
# ---------------------------------------------------------------------------


async def set_transaction_tags(
    db: AsyncSession,
    *,
    org_id: int,
    transaction_id: int,
    tag_names: Iterable[str],
    created_by_user_id: Optional[int],
) -> list[Tag]:
    """Replace the tag set on a transaction.

    Auto-creates any tag names that don't exist yet (inside the
    transaction's org). Each new tag triggers the same dictionary
    contribution path as direct ``create_tag`` calls.

    Enforces the per-transaction cap; raises ``ValidationError`` if the
    deduped normalized list exceeds it.
    """
    # Verify the transaction belongs to this org.
    tx_row = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.org_id == org_id,
        )
    )
    transaction = tx_row.scalar_one_or_none()
    if transaction is None:
        raise NotFoundError("transaction")

    # Normalize + dedupe before the cap check (a user typing "Insurance"
    # twice should count as one tag, not two).
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tag_names:
        n = normalize_tag_name(raw)
        if n in seen:
            continue
        seen.add(n)
        normalized.append(n)
    if len(normalized) > MAX_TAGS_PER_TRANSACTION:
        raise ValidationError(
            f"A transaction may have at most {MAX_TAGS_PER_TRANSACTION} tags"
        )

    # Resolve each normalized name to a Tag, creating new ones as needed.
    resolved_tags: list[Tag] = []
    for name_normalized in normalized:
        tag = await _get_tag_by_normalized(db, org_id, name_normalized)
        if tag is None:
            tag = await create_tag(
                db,
                org_id=org_id,
                name=name_normalized,
                created_by_user_id=created_by_user_id,
            )
        resolved_tags.append(tag)

    # Replace the join atomically: clear existing, re-insert the new set.
    await db.execute(
        delete(TransactionTag).where(
            TransactionTag.transaction_id == transaction_id
        )
    )
    for tag in resolved_tags:
        db.add(TransactionTag(transaction_id=transaction_id, tag_id=tag.id))

    return resolved_tags


# ---------------------------------------------------------------------------
# Cross-org dictionary contribution (write side)
# ---------------------------------------------------------------------------


async def _record_dictionary_contribution(
    db: AsyncSession,
    *,
    org_id: int,
    name_normalized: str,
) -> None:
    """Write side of the cross-org dictionary.

    Called only when ``share_tag_data=true`` for the org. Steps:

    1. Length / hyphen-group eligibility check; bail if not eligible.
    2. Upsert ``TagDictionary(name_normalized)`` to get the id.
    3. Insert ``TagDictionaryContributor(dictionary_tag_id, contributor_org_id)``.
       The unique constraint makes this a no-op when the org has
       contributed before.
    4. If the contributor row was newly created, increment
       ``contributor_org_count`` on the dictionary row.
    5. Always increment ``usage_count`` (popularity signal).

    All mutations are staged on the caller's session: commit is the
    caller's job (typically the router after the org-local tag create).
    """
    if not _is_dictionary_eligible(name_normalized):
        return

    # Step 2: upsert dictionary. SELECT short-circuits the common case
    # (the dictionary row already exists). When two opted-in orgs create
    # the same new tag concurrently, both threads see no row and try to
    # INSERT, which raises IntegrityError on the UNIQUE(name_normalized)
    # constraint. Wrap the INSERT in a SAVEPOINT so the collision rolls
    # back ONLY the savepoint, leaving the user's just-flushed Tag row in
    # the outer transaction intact. Pattern mirrors
    # ``_try_insert_contributor`` and existing usages in
    # transaction_service / routers/accounts / routers/admin_orgs.
    dictionary_tag = await _get_or_create_dictionary_row(db, name_normalized)

    # Step 3 + 4: insert contributor row, increment count if new.
    contributor_was_new = await _try_insert_contributor(
        db,
        dictionary_tag_id=dictionary_tag.id,
        contributor_org_id=org_id,
    )
    if contributor_was_new:
        dictionary_tag.contributor_org_count += 1

    # Step 5: usage_count increment is unconditional.
    dictionary_tag.usage_count += 1


async def _get_or_create_dictionary_row(
    db: AsyncSession,
    name_normalized: str,
) -> TagDictionary:
    """Fetch the ``tag_dictionary`` row, inserting it if missing.

    The INSERT is guarded by ``db.begin_nested()`` so a concurrent insert
    from another opted-in org colliding on
    ``UNIQUE(tag_dictionary.name_normalized)`` rolls back ONLY the
    savepoint. The outer request transaction (and the user's just-flushed
    Tag row) survives. After IntegrityError we re-SELECT to pick up the
    row the racing transaction committed.

    The retry SELECT uses ``with_for_update`` (FOR UPDATE) because under
    InnoDB's default REPEATABLE READ isolation a plain SELECT reads the
    snapshot established at transaction start, which does NOT see the
    row the racing transaction just committed. That would make
    ``scalar_one()`` raise ``NoResultFound`` and the user's local Tag
    would still get rolled back, defeating the savepoint. A locking
    read forces InnoDB to acquire a record lock against the secondary
    index and read the latest committed version, so the row is
    guaranteed visible. The lock is released at outer commit; for the
    contention pattern this guards (two opted-in orgs racing on a brand
    new generic tag), the lock window is the inserting savepoint plus
    the rest of the request handler, which is acceptable.

    Same pattern as ``_try_insert_contributor`` further down. The table
    differs but the failure mode is identical. The contributor path does
    NOT need the locking variant because it only returns a boolean
    ("did this org contribute?") and the IntegrityError already proves
    the answer is "yes" without needing to materialize the row.
    """
    existing = await db.execute(
        select(TagDictionary).where(
            TagDictionary.name_normalized == name_normalized
        )
    )
    dictionary_tag = existing.scalar_one_or_none()
    if dictionary_tag is not None:
        return dictionary_tag
    try:
        async with db.begin_nested():
            dictionary_tag = TagDictionary(
                name_normalized=name_normalized,
                contributor_org_count=0,
                usage_count=0,
                is_seed=False,
            )
            db.add(dictionary_tag)
            await db.flush()
        return dictionary_tag
    except IntegrityError:
        # Another opted-in org won the race. Re-fetch with a locking
        # read so we bypass the REPEATABLE READ snapshot and see the
        # row the racing transaction committed. Without this, the plain
        # SELECT would not see it and ``scalar_one()`` would raise
        # NoResultFound, rolling back the outer transaction.
        retry = await db.execute(
            select(TagDictionary)
            .where(TagDictionary.name_normalized == name_normalized)
            .with_for_update()
        )
        return retry.scalar_one()


async def _try_insert_contributor(
    db: AsyncSession,
    *,
    dictionary_tag_id: int,
    contributor_org_id: int,
) -> bool:
    """Insert a contributor row, returning True iff a NEW row was added.

    The unique constraint on ``(dictionary_tag_id, contributor_org_id)``
    is the dedupe mechanism. We do a SELECT-then-INSERT inside the same
    session: the SELECT short-circuits the common case (org has already
    contributed this tag) without forcing an IntegrityError round-trip,
    and the unique constraint is the second line of defence against
    races. A genuine race would be rare (same user creating the same tag
    twice in parallel sessions) and the IntegrityError path treats the
    duplicate as "already contributed".

    The contributor INSERT runs inside a SAVEPOINT (``db.begin_nested``)
    so an IntegrityError on a true race rolls back ONLY the savepoint,
    leaving the outer request transaction (and the user's just-flushed
    Tag row) intact. Without this guard, a previous version called
    ``await db.rollback()`` which silently discarded the user's tag
    create. Pattern matches existing usages in transaction_service /
    routers/accounts / routers/admin_orgs.
    """
    existing = await db.execute(
        select(TagDictionaryContributor.id).where(
            TagDictionaryContributor.dictionary_tag_id == dictionary_tag_id,
            TagDictionaryContributor.contributor_org_id == contributor_org_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return False
    try:
        async with db.begin_nested():
            db.add(TagDictionaryContributor(
                dictionary_tag_id=dictionary_tag_id,
                contributor_org_id=contributor_org_id,
            ))
            # Flush forces the INSERT now so the unique-constraint check
            # fires inside the savepoint scope.
            await db.flush()
    except IntegrityError:
        # Row inserted by a concurrent session. The savepoint already
        # rolled back so the outer transaction (and the user's Tag row)
        # is untouched. Treat as "already contributed", no count bump.
        return False
    return True


# ---------------------------------------------------------------------------
# Suggestion query (read side)
# ---------------------------------------------------------------------------


async def suggest_tags(
    db: AsyncSession,
    *,
    org_id: int,
    prefix: Optional[str],
    category_id: Optional[int],
    limit: int,
) -> list[TagSuggestion]:
    """Three-pass suggestion query.

    Pass 1 (``org_co_category``): tags this org has used on transactions
    in the given category, ranked by frequency. Skipped when
    ``category_id`` is None.

    Pass 2 (``org_recent``): any of this org's tags whose normalized
    name matches the prefix, ranked by overall usage. Used to top up
    after pass 1 and as the only org-local pass when no category is
    given.

    Pass 3 (``shared_dictionary``): cross-org dictionary entries past
    the k-anonymity floor (or seeded). Skipped if the org has not opted
    into ``share_tag_data``.

    The three passes are deduped against each other by tag *name*.
    """
    if limit <= 0:
        return []
    prefix_norm = (prefix or "").strip().lower()
    if prefix_norm:
        # We use LIKE 'prefix%' because the leading-column index on
        # name_normalized covers prefix scans. Substring matches would
        # miss the index and degrade as the table grows.
        like_clause = f"{prefix_norm}%"
    else:
        like_clause = "%"  # Empty prefix returns top-N overall.

    suggestions: list[TagSuggestion] = []
    seen_names: set[str] = set()

    # Pass 1: org_co_category.
    if category_id is not None:
        co_query = (
            select(
                Tag.name,
                Tag.name_normalized,
                func.count(Transaction.id).label("weight"),
            )
            .join(TransactionTag, TransactionTag.tag_id == Tag.id)
            .join(Transaction, Transaction.id == TransactionTag.transaction_id)
            .where(
                Tag.org_id == org_id,
                Transaction.org_id == org_id,
                Transaction.category_id == category_id,
                Tag.name_normalized.like(like_clause),
            )
            .group_by(Tag.id)
            .order_by(
                func.count(Transaction.id).desc(),
                Tag.name_normalized.asc(),
            )
            .limit(limit)
        )
        rows = (await db.execute(co_query)).all()
        for name, name_normalized, weight in rows:
            if name_normalized in seen_names:
                continue
            seen_names.add(name_normalized)
            suggestions.append(TagSuggestion(
                name=name, source="org_co_category", weight=int(weight)
            ))
            if len(suggestions) >= limit:
                return suggestions

    # Pass 2: org_recent (any of this org's tags matching the prefix).
    remaining = limit - len(suggestions)
    if remaining > 0:
        recent_query = (
            select(
                Tag.name,
                Tag.name_normalized,
                func.count(TransactionTag.tag_id).label("weight"),
            )
            .outerjoin(TransactionTag, TransactionTag.tag_id == Tag.id)
            .where(
                Tag.org_id == org_id,
                Tag.name_normalized.like(like_clause),
            )
            .group_by(Tag.id)
            .order_by(
                func.count(TransactionTag.tag_id).desc(),
                Tag.name_normalized.asc(),
            )
            .limit(remaining + len(seen_names))
        )
        rows = (await db.execute(recent_query)).all()
        for name, name_normalized, weight in rows:
            if name_normalized in seen_names:
                continue
            seen_names.add(name_normalized)
            suggestions.append(TagSuggestion(
                name=name, source="org_recent", weight=int(weight)
            ))
            if len(suggestions) >= limit:
                return suggestions

    # Pass 3: shared_dictionary (gated on opt-in + k-anonymity floor).
    remaining = limit - len(suggestions)
    if remaining > 0 and await is_share_tag_data_enabled(db, org_id):
        dict_query = (
            select(
                TagDictionary.name_normalized,
                TagDictionary.usage_count,
            )
            .where(
                TagDictionary.name_normalized.like(like_clause),
                or_(
                    TagDictionary.contributor_org_count
                    >= SHARED_DICTIONARY_MIN_CONTRIBUTORS,
                    TagDictionary.is_seed.is_(True),
                ),
            )
            .order_by(
                TagDictionary.usage_count.desc(),
                TagDictionary.name_normalized.asc(),
            )
            .limit(remaining + len(seen_names))
        )
        rows = (await db.execute(dict_query)).all()
        for name_normalized, usage_count in rows:
            if name_normalized in seen_names:
                continue
            seen_names.add(name_normalized)
            suggestions.append(TagSuggestion(
                name=name_normalized,
                source="shared_dictionary",
                weight=int(usage_count or 0),
            ))
            if len(suggestions) >= limit:
                return suggestions

    return suggestions
