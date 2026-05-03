"""Import service — orchestrates CSV preview and transaction creation.

Parsing/validation is separate from persistence so a background worker
can replace the synchronous confirm path later without a rewrite.
"""

from collections import Counter

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.settings import OrgSetting
from app.models.transaction import Transaction, TransactionType
from app.schemas.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewRow,
    ImportPreviewResponse,
    ImportRowError,
)
from app.schemas.transaction import (
    DuplicateCandidate,
    TransactionCreate,
    TransferCandidate,
)
from app.services import transaction_service
from app.services.transaction_service import (
    find_duplicate_of_linked_leg,
    find_match_candidates,
)
from app.services.category_rules_service import (
    bump_shared_vote,
    infer_category,
    learn_from_choice,
    normalize_description,
    should_skip_learning,
)
from app.services.import_parser import ParsedRow

logger = structlog.get_logger()


async def build_preview(
    db: AsyncSession,
    org_id: int,
    account_id: int,
    file_name: str,
    parsed_rows: list[ParsedRow],
) -> ImportPreviewResponse:
    """Build a preview response: flag duplicates and potential transfers."""

    # Validate account belongs to this org and load it for currency.
    # Detectors filter cross-account candidates by Account.currency; we need
    # the destination's currency to keep the match space sensible.
    destination_account = await db.scalar(
        select(Account).where(Account.id == account_id, Account.org_id == org_id)
    )
    if destination_account is None:
        # Match validate_account semantics: surface the same error shape.
        await transaction_service.validate_account(db, account_id, org_id)

    # ── Batch duplicate check: single query for the CSV date range ──
    min_date = min(r.date for r in parsed_rows)
    max_date = max(r.date for r in parsed_rows)
    existing_result = await db.execute(
        select(
            Transaction.id, Transaction.date, Transaction.amount, Transaction.description
        ).where(
            and_(
                Transaction.org_id == org_id,
                Transaction.account_id == account_id,
                Transaction.date.between(min_date, max_date),
            )
        )
    )
    existing_map: dict[tuple, int] = {
        (row.date, row.amount, row.description): row.id
        for row in existing_result.all()
    }

    preview_rows: list[ImportPreviewRow] = []
    duplicate_count = 0

    for row in parsed_rows:
        dup_id = existing_map.get((row.date, row.amount, row.description))
        is_dup = dup_id is not None

        # ── Smart-rules suggestion ──
        suggested_category_id, suggestion_source = await infer_category(
            db, org_id=org_id, description=row.description
        )

        if is_dup:
            duplicate_count += 1

        # ── Detector 1: same-account already-linked match → flag duplicate-of-linked-leg ──
        row_tx_type = (
            TransactionType.EXPENSE if row.type == "expense" else TransactionType.INCOME
        )
        dup_linked_candidates = await find_duplicate_of_linked_leg(
            db, org_id,
            account_id=account_id,
            amount=row.amount,
            type=row_tx_type,
            date=row.date,
            currency=destination_account.currency,
        )
        is_dup_of_linked = bool(dup_linked_candidates)
        duplicate_candidate_obj: DuplicateCandidate | None = None
        if is_dup_of_linked:
            c0 = dup_linked_candidates[0]
            duplicate_candidate_obj = DuplicateCandidate(
                id=c0.id,
                date=c0.date,
                description=c0.description,
                amount=c0.amount,
                account_id=c0.account_id,
                account_name=c0.account.name,
                existing_leg_is_imported=c0.is_imported,
            )

        # ── Detector 2: cross-account un-linked match → suggest transfer pair ──
        # Skipped when Detector 1 fires — a duplicate-of-linked-leg already has
        # an answer (default action: drop), so cross-account suggestions would
        # only confuse the user.
        if is_dup_of_linked:
            action: str = "none"
            confidence: str | None = None
            pair_with_id: int | None = None
            candidate_models: list[TransferCandidate] = []
        else:
            match_candidates = await find_match_candidates(
                db, org_id,
                source_type=row_tx_type,
                amount=row.amount,
                account_id_excluded=account_id,
                date=row.date,
                currency=destination_account.currency,
            )
            if not match_candidates:
                action, confidence, pair_with_id, candidate_models = (
                    "none", None, None, [],
                )
            elif len(match_candidates) >= 2:
                action = "choose_candidate"
                confidence = "multi_candidate"
                pair_with_id = None
                candidate_models = [
                    TransferCandidate(
                        id=c.id,
                        date=c.date,
                        description=c.description,
                        amount=c.amount,
                        account_id=c.account_id,
                        account_name=c.account.name,
                        date_diff_days=abs((c.date - row.date).days),
                        confidence="same_day" if c.date == row.date else "near_date",
                    )
                    for c in match_candidates
                ]
            else:
                c0 = match_candidates[0]
                diff = abs((c0.date - row.date).days)
                if diff == 0:
                    action, confidence = "pair_with", "same_day"
                else:
                    action, confidence = "suggest_pair", "near_date"
                pair_with_id = c0.id
                candidate_models = []

        preview_rows.append(
            ImportPreviewRow(
                row_number=row.row_number,
                date=row.date,
                description=row.description,
                amount=row.amount,
                type=row.type,
                counterparty=row.counterparty,
                transaction_type=row.transaction_type,
                is_duplicate=is_dup,
                duplicate_transaction_id=dup_id,
                suggested_category_id=suggested_category_id,
                suggestion_source=suggestion_source,
                # Detector 1 outputs.
                is_duplicate_of_linked_leg=is_dup_of_linked,
                duplicate_candidate=duplicate_candidate_obj,
                default_action_drop=is_dup_of_linked,
                # Detector 2 outputs.
                transfer_match_action=action,
                transfer_match_confidence=confidence,
                pair_with_transaction_id=pair_with_id,
                transfer_candidates=candidate_models,
            )
        )

    # ── Aggregate smart-rules metric (architect-mandated; one event per preview) ──
    source_split = Counter((r.suggestion_source or "skipped") for r in preview_rows)
    suggested_count = sum(
        1 for r in preview_rows if r.suggested_category_id is not None
    )
    await logger.ainfo(
        "smart_rules.preview_built",
        org_id=org_id,
        rows_total=len(preview_rows),
        suggested_count=suggested_count,
        source_split=dict(source_split),
    )

    # ── Detector summary counters + telemetry (spec §3.2) ──
    auto_paired_count = sum(
        1 for r in preview_rows if r.transfer_match_action == "pair_with"
    )
    suggested_pair_count = sum(
        1 for r in preview_rows if r.transfer_match_action == "suggest_pair"
    )
    multi_candidate_count = sum(
        1 for r in preview_rows if r.transfer_match_action == "choose_candidate"
    )
    duplicate_of_linked_count = sum(
        1 for r in preview_rows if r.is_duplicate_of_linked_leg
    )
    await logger.ainfo(
        "import.preview.matched",
        org_id=org_id,
        file_name=file_name,
        auto_paired=auto_paired_count,
        suggested=suggested_pair_count,
        multi_candidate=multi_candidate_count,
        duplicate_of_linked=duplicate_of_linked_count,
    )

    return ImportPreviewResponse(
        rows=preview_rows,
        account_id=account_id,
        file_name=file_name,
        total_rows=len(preview_rows),
        duplicate_count=duplicate_count,
        auto_paired_count=auto_paired_count,
        suggested_pair_count=suggested_pair_count,
        multi_candidate_count=multi_candidate_count,
        duplicate_of_linked_count=duplicate_of_linked_count,
    )


async def execute_import(
    db: AsyncSession,
    org_id: int,
    body: ImportConfirmRequest,
) -> ImportConfirmResponse:
    """Create transactions for all confirmed (non-skipped) rows.

    Uses the existing create_transaction / create_transfer service functions
    so all validation, balance mutations, and locking are preserved.
    """
    imported_count = 0
    skipped_count = 0
    errors: list[ImportRowError] = []

    # ── Smart-rules learning: fetch share flag once, init aggregate counters ──
    share_flag = (await db.execute(
        select(OrgSetting.value).where(
            OrgSetting.org_id == org_id,
            OrgSetting.key == "share_merchant_data",
        )
    )).scalar_one_or_none()
    share_merchant_data = (share_flag == "true")

    learned_count = 0
    accepted_count = 0
    overridden_count = 0
    source_split: Counter[str] = Counter()
    miss_tokens: set[str] = set()

    for row in body.rows:
        if row.skip:
            skipped_count += 1
            continue

        category_id = row.category_id or body.default_category_id

        try:
            # PR-C C1: only the create branch is wired. The pair_with_existing
            # and drop_as_duplicate branches land in C3.
            tx_body = TransactionCreate(
                account_id=body.account_id,
                category_id=category_id,
                description=row.description,
                amount=row.amount,
                type=row.type,
                date=row.date,
            )
            await transaction_service.create_transaction(
                db, org_id, tx_body, is_imported=True
            )

            # ── Learn from the user's category choice ────────────────
            # Skip transfers (linked rows) and rows with no category at
            # all. The double commit (create_transaction commits the txn,
            # this block commits the rule + vote separately) is
            # intentional — keeps a learn-failure from rolling back the
            # imported transaction.
            if row.category_id is not None and not should_skip_learning(row):
                accepted = (
                    row.suggested_category_id is not None
                    and row.suggested_category_id == row.category_id
                )
                source = "user_pick" if accepted else "user_edit"
                # Learning is best-effort: a failure here must NOT
                # bubble out as a row error. The transaction itself
                # has already committed (see create_transaction
                # above) — the row is imported regardless.
                try:
                    await learn_from_choice(
                        db,
                        org_id=org_id,
                        description=row.description,
                        category_id=row.category_id,
                        source=source,
                    )
                    if (
                        accepted and share_merchant_data
                        and row.suggestion_source == "shared_dictionary"
                    ):
                        await bump_shared_vote(db, description=row.description)
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    await logger.awarning(
                        "smart_rules.learn_failed",
                        org_id=org_id,
                        op="execute_import",
                        row_number=row.row_number,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )

                # Counters always update — the user's choice was
                # registered against an imported row, even if the
                # rule write failed and got logged above.
                learned_count += 1
                if accepted:
                    accepted_count += 1
                elif row.suggested_category_id is not None:
                    overridden_count += 1

            # Metric collection — fires for EVERY imported non-transfer
            # row, including default-category fallthroughs (row.category_id
            # is None and the user relied on default_category_id). This
            # is the architect-mandated signal for "uncategorizable on
            # import" and must NOT be gated on row.category_id.
            if not should_skip_learning(row):
                source_split[row.suggestion_source or "default"] += 1
                if row.suggestion_source in ("default", None):
                    token = normalize_description(row.description)
                    if token:
                        miss_tokens.add(token)

            imported_count += 1

        except Exception as exc:
            await db.rollback()
            await logger.awarning(
                "import_row_failed",
                row_number=row.row_number,
                error=str(exc),
            )
            errors.append(ImportRowError(row_number=row.row_number, error=str(exc)))

    # ── Aggregate smart-rules metric (architect-mandated; one per import) ──
    await logger.ainfo(
        "smart_rules.import_executed",
        org_id=org_id,
        rows_total=len(body.rows),
        imported_count=imported_count,
        learned_count=learned_count,
        accepted_count=accepted_count,
        overridden_count=overridden_count,
        source_split=dict(source_split),
        miss_count=len(miss_tokens),
    )
    # Per-UNIQUE-token miss events (set-dedup is load-bearing — emitting
    # per-row would flood the metric with duplicates of the same merchant).
    for token in miss_tokens:
        await logger.ainfo(
            "smart_rules.miss", org_id=org_id, normalized_token=token,
        )

    return ImportConfirmResponse(
        imported_count=imported_count,
        skipped_count=skipped_count,
        error_count=len(errors),
        errors=errors,
    )
