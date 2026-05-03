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
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.transaction_service import (
    _create_transaction_no_commit,
    _link_pair,
    _load_opts,
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
    """Create / pair / drop transactions for all confirmed (non-skipped) rows.

    Per-row branches (spec §3.3):
      action="create"             → plain insert via _create_transaction_no_commit.
      action="pair_with_existing" → insert new leg + atomically link to a
                                    locked partner via _link_pair. Single
                                    nested savepoint per row guarantees
                                    rollback on any failure inside the pair.
      action="drop_as_duplicate"  → server-side revalidate the duplicate-of-
                                    linked-leg candidate, then skip insert.

    Each row runs inside its own ``db.begin_nested()`` savepoint so failures
    in the pair branch (e.g. _link_pair raises after the new leg flushed)
    cleanly roll back the partial work without taking the whole batch with it.
    Smart-rules learning runs OUTSIDE the savepoint per row, best-effort:
    a learn failure must NOT bubble out as a row error or roll back the
    imported transaction.
    """
    imported_count = 0          # plain creates only (action="create")
    paired_count = 0
    dropped_duplicate_count = 0
    skipped_count = 0
    errors: list[ImportRowError] = []

    # Destination account for currency lookups (used by both pair partner
    # validation and drop_as_duplicate revalidation).
    destination_account = await db.scalar(
        select(Account).where(
            Account.id == body.account_id, Account.org_id == org_id
        )
    )
    if destination_account is None:
        await transaction_service.validate_account(db, body.account_id, org_id)

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
    paired_pair_ids: list[tuple[int, int]] = []

    for row in body.rows:
        if row.skip:
            skipped_count += 1
            continue

        category_id = row.category_id or body.default_category_id
        action_taken: str | None = None
        new_tx: Transaction | None = None

        try:
            async with db.begin_nested():
                if row.action == "create":
                    tx_body = TransactionCreate(
                        account_id=body.account_id,
                        category_id=category_id,
                        description=row.description,
                        amount=row.amount,
                        type=row.type,
                        date=row.date,
                    )
                    new_tx = await _create_transaction_no_commit(
                        db, org_id, tx_body, is_imported=True
                    )
                    action_taken = "create"

                elif row.action == "pair_with_existing":
                    if row.pair_with_transaction_id is None:
                        raise ValidationError(
                            "pair_with_transaction_id required when "
                            "action='pair_with_existing'"
                        )
                    tx_body = TransactionCreate(
                        account_id=body.account_id,
                        category_id=category_id,
                        description=row.description,
                        amount=row.amount,
                        type=row.type,
                        date=row.date,
                    )
                    new_tx = await _create_transaction_no_commit(
                        db, org_id, tx_body, is_imported=True
                    )

                    # Lock partner row with FOR UPDATE + populate_existing so
                    # the eligibility re-check reads the freshest server state
                    # (defends against a concurrent pair landing between
                    # preview and confirm).
                    partner_locked = await db.execute(
                        select(Transaction)
                        .options(*_load_opts())
                        .where(
                            Transaction.id == row.pair_with_transaction_id,
                            Transaction.org_id == org_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    partner = partner_locked.scalar_one_or_none()
                    if partner is None:
                        raise ConflictError(
                            "Partner row no longer exists; re-preview"
                        )
                    if partner.linked_transaction_id is not None:
                        raise ConflictError(
                            "Partner row is already linked; re-preview"
                        )
                    if partner.recurring_id is not None:
                        raise ConflictError(
                            "Partner row is recurring; re-preview"
                        )
                    if partner.account.currency != destination_account.currency:
                        raise ConflictError(
                            "Currency mismatch detected at confirm; re-preview"
                        )

                    # Determine which leg is expense / income.
                    if new_tx.type == TransactionType.EXPENSE:
                        expense_tx, income_tx = new_tx, partner
                    else:
                        expense_tx, income_tx = partner, new_tx

                    await _link_pair(
                        db,
                        expense_tx=expense_tx,
                        income_tx=income_tx,
                        recategorize=row.recategorize,
                        transfer_category_id=row.transfer_category_id,
                    )
                    paired_pair_ids.append((new_tx.id, partner.id))
                    action_taken = "pair_with_existing"

                elif row.action == "drop_as_duplicate":
                    if row.duplicate_of_transaction_id is None:
                        raise ValidationError(
                            "duplicate_of_transaction_id required when "
                            "action='drop_as_duplicate'"
                        )
                    row_tx_type = (
                        TransactionType.EXPENSE
                        if row.type == "expense"
                        else TransactionType.INCOME
                    )
                    dup_check = await find_duplicate_of_linked_leg(
                        db, org_id,
                        account_id=body.account_id,
                        amount=row.amount,
                        type=row_tx_type,
                        date=row.date,
                        currency=destination_account.currency,
                    )
                    candidate_ids = {c.id for c in dup_check}
                    if row.duplicate_of_transaction_id not in candidate_ids:
                        raise ConflictError(
                            "Duplicate candidate no longer matches; re-preview"
                        )
                    action_taken = "drop_as_duplicate"

        except (ConflictError, ValidationError, NotFoundError) as exc:
            # Domain failures: row-level error, batch continues.
            errors.append(
                ImportRowError(row_number=row.row_number, error=str(exc))
            )
            await logger.awarning(
                "import_row_failed",
                row_number=row.row_number,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            continue
        except Exception as exc:
            # Unexpected failures: surface as row-level error, batch continues.
            errors.append(
                ImportRowError(row_number=row.row_number, error=str(exc))
            )
            await logger.awarning(
                "import_row_failed",
                row_number=row.row_number,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            continue

        # Per-action counter increments + telemetry (post-savepoint commit).
        if action_taken == "create":
            imported_count += 1
        elif action_taken == "pair_with_existing":
            paired_count += 1
            await logger.ainfo(
                "transfers.linked",
                org_id=org_id,
                expense_id=expense_tx.id,
                income_id=income_tx.id,
                source="import_pair",
                recategorized=row.recategorize,
            )
        elif action_taken == "drop_as_duplicate":
            dropped_duplicate_count += 1
            await logger.ainfo(
                "import.dropped_duplicate_leg",
                org_id=org_id,
                csv_row_index=row.row_number,
                duplicate_of_transaction_id=row.duplicate_of_transaction_id,
                account_id=body.account_id,
                amount=str(row.amount),  # spec §6.1: amount as string
            )

        # ── Best-effort learning from the user's category choice ───────────
        # Runs OUTSIDE the per-row savepoint so a learn failure doesn't undo
        # the transaction. Skipped for paired rows (transfer legs — the
        # description is bank-noise, not a meaningful merchant) and dropped
        # rows (no transaction was created).
        if action_taken == "create" and row.category_id is not None and not should_skip_learning(new_tx):
            accepted = (
                row.suggested_category_id is not None
                and row.suggested_category_id == row.category_id
            )
            source = "user_pick" if accepted else "user_edit"
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

            # Counters always update — the user's choice was registered
            # against an imported row, even if the rule write failed.
            learned_count += 1
            if accepted:
                accepted_count += 1
            elif row.suggested_category_id is not None:
                overridden_count += 1

        # Metric collection — fires for EVERY imported plain-create row,
        # including default-category fallthroughs (row.category_id is None
        # and the user relied on default_category_id). Architect-mandated
        # "uncategorizable on import" signal; must NOT be gated on
        # row.category_id. Paired/dropped rows are excluded here because
        # paired rows aren't "missing a merchant rule" (they're transfers)
        # and dropped rows didn't create a transaction.
        if action_taken == "create" and not should_skip_learning(new_tx):
            source_split[row.suggestion_source or "default"] += 1
            if row.suggestion_source in ("default", None):
                token = normalize_description(row.description)
                if token:
                    miss_tokens.add(token)

    # Final commit for any savepoints whose changes haven't been flushed to
    # the outer transaction yet (savepoint commit only releases to the outer
    # transaction; we still owe the outer commit).
    await db.commit()

    # ── Aggregate smart-rules metric (architect-mandated; one per import) ──
    # rows_total preserves its original meaning: total submitted rows.
    await logger.ainfo(
        "smart_rules.import_executed",
        org_id=org_id,
        rows_total=len(body.rows),
        imported_count=imported_count,
        paired_count=paired_count,
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

    # ── Per-execute confirm summary (transfer-aware) ──
    await logger.ainfo(
        "import.confirmed.transfers",
        org_id=org_id,
        paired_count=paired_count,
        dropped_duplicate_count=dropped_duplicate_count,
        plain_created_count=imported_count,
    )

    return ImportConfirmResponse(
        imported_count=imported_count,
        paired_count=paired_count,
        dropped_duplicate_count=dropped_duplicate_count,
        skipped_count=skipped_count,
        error_count=len(errors),
        errors=errors,
    )
