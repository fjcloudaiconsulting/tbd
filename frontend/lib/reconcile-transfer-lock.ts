import type { ReconciliationState } from "@/lib/types";

/**
 * Targets the reconcile inbox must NOT offer on a row that is one leg of a
 * real (mutually linked) transfer, because the server refuses all four.
 *
 * ⚠ THIS MIRRORS THE SERVER AND IS FENCED. `matched` and `edited` are refused
 * by `_apply_match` guard 2 and `_apply_edits`; `skipped` and `rejected` by the
 * TBD-385 guard in `_reconcile_one`, which derives its roster from
 * `transaction_filters.REVERTED_RECONCILIATION_STATES` and therefore extends
 * AUTOMATICALLY if a third reverting state is ever added. This list does not.
 *
 * That asymmetry is the whole reason this constant lives in `lib/` rather than
 * inline in `ReconcileClient.tsx`: `frontend/lib` is mounted into the backend
 * container, so `backend/tests/test_reconcile_transfer_lock_frontend_contract.py`
 * can read it and fail when the two sides disagree. Inline, a new reverting
 * state would be refused by the server while the inbox kept offering the
 * button -- re-creating the exact "the UI offers an action the server rejects"
 * violation TBD-385 was written to remove.
 */
export const TRANSFER_LOCKED_TARGETS: ReconciliationState[] = [
  "matched",
  "edited",
  "skipped",
  "rejected",
];
