---
name: _create_transaction_no_commit double-lock optimization (deferred)
description: Pre-lock pattern in convert_and_create_leg + create_transfer_pair causes _create_transaction_no_commit to re-enter the account lock internally. Acceptable today; cleanup opportunity is a no-lock "create with already-locked account" helper.
type: project
originSessionId: c08a4469-7a56-42db-a6cd-d380693d40d3
---
**Status:** Deferred follow-up. Non-blocking. Flagged 2026-05-03 by reviewer during PR #118 final pass.

## What's happening

Two service functions sort-pre-lock affected accounts via `get_account_for_update` to prevent deadlocks under concurrent opposite-direction operations:

- `convert_and_create_leg` (transaction_service.py) — pre-locks src + dst accounts before the create.
- `create_transfer_pair` import branch (import_service.py) — same pattern: pre-locks both accounts before either `_create_transaction_no_commit` call.

After the pre-lock, `_create_transaction_no_commit` is called twice (once per leg in the transfer-pair case). Internally it ALSO calls `get_account_for_update(body.account_id, ...)` for SETTLED rows. Inside the same DB transaction, re-acquiring `SELECT ... FOR UPDATE` on an already-held row is a no-op for correctness — but it's still an extra round trip per leg.

## Cost

For a 1000-row import where 200 rows use `create_transfer_pair`, that's 200 × 2 = 400 redundant FOR UPDATE round trips. Tiny per-call cost; cumulative impact depends on row counts and network latency to MySQL. Probably negligible for solo-user workloads but visible if measured.

## Cleanup options

**Option A (cleanest):** Add a sibling primitive `_create_transaction_no_commit_with_account(db, org_id, body, account, *, is_imported=False)` that takes an already-loaded `Account` object and skips the internal `get_account_for_update`. Used by callers that have pre-locked. Existing `_create_transaction_no_commit` stays as the public-wrapper-friendly variant.

**Option B (overload):** Add an optional `account: Account | None = None` parameter to `_create_transaction_no_commit`; when set, skip the lock acquisition. Slightly muddier contract.

**Option C (no-op):** Leave it. The double-lock is harmless and the redundant round trip is small.

## When to fix

Not a launch blocker. Revisit if:
- Import performance becomes a real complaint at scale.
- We add another caller that pre-locks (3+ callers makes the helper extraction worthwhile).
- Database round-trip latency becomes a measurable cost (e.g., move from local MySQL to a remote managed instance — relevant once `project_infra_cost_reduction.md` ships).

## Cross-refs

- `convert_and_create_leg` — backend/app/services/transaction_service.py (added in PR-B, lock-fix in PR-B review).
- `create_transfer_pair` import branch — backend/app/services/import_service.py (added in PR #118).
- `_create_transaction_no_commit` — backend/app/services/transaction_service.py (added in PR-A Task A8).
