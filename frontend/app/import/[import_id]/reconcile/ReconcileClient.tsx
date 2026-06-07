"use client";

import { useCallback, useMemo, useState, type FormEvent } from "react";
import useSWR from "swr";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import Spinner from "@/components/ui/Spinner";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  badgeError,
  badgeInfo,
  badgeNeutral,
  badgeSuccess,
  badgeWarning,
  btnLink,
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
} from "@/lib/styles";
import type {
  ImportBatchDetail,
  ReconciliationRow,
  ReconciliationState,
  ReconciliationTransition,
} from "@/lib/types";

// L3.2 Wave 2B: post-import reconciliation inbox client island.
//
// Renders the batch header (file, source format, row counters, progress
// pill) and a per-row table with action buttons (Accept, Skip, Edit,
// Reject). The state machine is server-authoritative; this UI only
// fires single-transition requests via /api/v1/import/{batchId}/reconcile
// and trusts the server response (revalidating SWR so any side effects
// land).
//
// The duplicate-warning callout is shown when a row's FITID matches a
// transaction outside this batch. It's informational, not blocking:
// the user can still accept the row to override the warning.

// Map a reconciliation state to a visually distinct badge variant.
// Matches the existing badge scale: warning for pending/unmatched,
// info for in-progress (matched/edited), success for accepted,
// error for rejected, neutral for skipped.
const STATE_BADGE: Record<ReconciliationState, string> = {
  pending_review: badgeWarning,
  unmatched: badgeWarning,
  matched: badgeInfo,
  edited: badgeInfo,
  accepted: badgeSuccess,
  rejected: badgeError,
  skipped: badgeNeutral,
};

const STATE_LABEL: Record<ReconciliationState, string> = {
  pending_review: "Pending review",
  unmatched: "Unmatched",
  matched: "Matched",
  edited: "Edited",
  accepted: "Accepted",
  rejected: "Rejected",
  skipped: "Skipped",
};

// Server-mirrored allowed-transitions. The recon UI only offers
// buttons for transitions the server will actually accept, so the user
// never sees a 409 from the inbox.
const ALLOWED_NEXT: Record<ReconciliationState, ReconciliationState[]> = {
  // PR #247 P1: Match + Edit are now first-class actions from a
  // pending row. Order matters for visual hierarchy: primary action
  // (Accept) first, secondary (Edit / Match / Skip) middle, danger
  // (Reject) last.
  pending_review: ["accepted", "edited", "matched", "skipped", "rejected"],
  unmatched: ["accepted", "edited", "matched", "skipped", "rejected"],
  matched: ["accepted"],
  edited: ["accepted"],
  // Terminal-ish states: only Accepted permits a reopen.
  accepted: ["pending_review"],
  rejected: [],
  skipped: [],
};

// Human-friendly button labels keyed by the TARGET state.
const ACTION_LABEL: Record<ReconciliationState, string> = {
  pending_review: "Reopen",
  unmatched: "Mark unmatched",
  matched: "Match",
  edited: "Edit",
  accepted: "Accept",
  rejected: "Reject",
  skipped: "Skip",
};

function formatDate(iso: string): string {
  // Server returns ``YYYY-MM-DD``; the recon UI is read-only here, so
  // we render it as a localized date but fall back to the raw string
  // if the locale parser bails.
  try {
    const d = new Date(`${iso}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function formatAmount(amount: string, type: "income" | "expense"): string {
  const sign = type === "income" ? "+" : "-";
  return `${sign}${amount}`;
}

type RowActionState = {
  busy: boolean;
  error: string | null;
};

export default function ReconcileClient({
  batchId,
  initialBatch,
}: {
  batchId: number;
  initialBatch: ImportBatchDetail | null;
}) {
  const swrKey = `/api/v1/import/${batchId}`;
  const { data, error, isLoading, mutate } = useSWR<ImportBatchDetail>(
    swrKey,
    (path: string) => apiFetch<ImportBatchDetail>(path),
    {
      fallbackData: initialBatch ?? undefined,
      revalidateOnFocus: false,
    },
  );

  const [rowState, setRowState] = useState<Record<number, RowActionState>>(
    {},
  );
  const [globalError, setGlobalError] = useState<string | null>(null);
  // PR #247 P1: modal state for the new Edit + Match actions. Only one
  // modal is open at a time; ``modalRow`` carries the row payload so
  // the dialog can prefill description / amount / date / category.
  const [editModalRow, setEditModalRow] =
    useState<ReconciliationRow | null>(null);
  const [matchModalRow, setMatchModalRow] =
    useState<ReconciliationRow | null>(null);

  const applyTransition = useCallback(
    async (
      transactionId: number,
      to: ReconciliationState,
      extras: Partial<Omit<ReconciliationTransition, "transaction_id" | "to_state">> = {},
    ) => {
      setGlobalError(null);
      setRowState((prev) => ({
        ...prev,
        [transactionId]: { busy: true, error: null },
      }));
      try {
        await apiFetch(
          `/api/v1/import/${batchId}/reconcile`,
          {
            method: "POST",
            body: JSON.stringify({
              transitions: [
                {
                  transaction_id: transactionId,
                  to_state: to,
                  ...extras,
                },
              ],
            }),
          },
        );
        await mutate();
        setRowState((prev) => ({
          ...prev,
          [transactionId]: { busy: false, error: null },
        }));
      } catch (err) {
        const msg = extractErrorMessage(err, "Action failed");
        setRowState((prev) => ({
          ...prev,
          [transactionId]: { busy: false, error: msg },
        }));
        setGlobalError(msg);
      }
    },
    [batchId, mutate],
  );

  const batch = data?.batch ?? null;
  const rows: ReconciliationRow[] = data?.rows ?? [];
  const progress = useMemo(() => {
    if (!batch || batch.total_rows === 0) return { done: 0, total: 0 };
    const done = batch.total_rows - batch.pending_count;
    return { done, total: batch.total_rows };
  }, [batch]);

  // ── Header / state-aware framing ──
  return (
    <AppShell>
      <div className="mx-auto max-w-5xl px-4 py-6 sm:px-8">
        <div className="mb-6 flex items-start gap-2">
          <h1 className={pageTitle}>Reconcile import</h1>
          <HelpAnchor
            section="import-reconcile"
            label="Import reconciliation"
            variant="inline-title"
          />
        </div>

        {/* ── ERROR / NOT-FOUND ──────────────────────────────────────── */}
        {error || (!isLoading && !batch) ? (
          <div className={card}>
            <div className="px-6 py-10 text-center">
              <p className="mb-2 text-sm text-text-secondary">
                We could not load this import batch.
              </p>
              <p className={errorCls}>
                {globalError ??
                  "The batch may have been deleted, or you do not have access."}
              </p>
              <a href="/import" className={`${btnLink} mt-4 inline-block`}>
                Back to import
              </a>
            </div>
          </div>
        ) : null}

        {/* ── LOADING ────────────────────────────────────────────────── */}
        {isLoading && !batch ? (
          <div className={card}>
            <div className="flex items-center gap-3 px-6 py-10">
              <Spinner />
              <span className="text-sm text-text-muted">
                Loading batch details...
              </span>
            </div>
          </div>
        ) : null}

        {/* ── LOADED ─────────────────────────────────────────────────── */}
        {batch ? (
          <>
            {/* Header card with progress + counters */}
            <div className={`${card} relative mb-6`}>
              <div className={cardHeader}>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className={cardTitle}>
                      {batch.source_format.toUpperCase()} import
                    </p>
                    <p className="mt-1 break-all text-sm font-medium text-text-primary">
                      {batch.file_name}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {batch.status === "closed" ? (
                      <span className={badgeSuccess}>
                        Batch closed
                      </span>
                    ) : (
                      <span className={badgeInfo}>Open</span>
                    )}
                  </div>
                </div>
              </div>
              <div className="px-6 py-4">
                <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 text-sm">
                  <div>
                    <span className="text-text-muted">Reconciled</span>
                    <span className="ml-2 font-medium text-text-primary">
                      {progress.done} of {progress.total}
                    </span>
                  </div>
                  <div>
                    <span className="text-text-muted">Pending</span>
                    <span className="ml-2 font-medium text-text-primary">
                      {batch.pending_count}
                    </span>
                  </div>
                </div>
                {/* Visual progress bar -- uses the same accent palette as
                    the rest of the app; light + dark via token classes. */}
                <div
                  className="mt-3 h-2 w-full overflow-hidden rounded-full bg-surface-raised"
                  role="progressbar"
                  aria-valuenow={progress.done}
                  aria-valuemin={0}
                  aria-valuemax={progress.total}
                  aria-label="Reconciliation progress"
                >
                  <div
                    className="h-full bg-accent transition-[width]"
                    style={{
                      width:
                        progress.total === 0
                          ? "0%"
                          : `${Math.round((progress.done / progress.total) * 100)}%`,
                    }}
                  />
                </div>
              </div>
            </div>

            {/* Global error banner from the last failed action. */}
            {globalError ? (
              <div className={`${errorCls} mb-4`}>{globalError}</div>
            ) : null}

            {/* Row table -- list shape on desktop, stacked cards on
                mobile via the responsive grid. */}
            {rows.length === 0 ? (
              <div className={card}>
                <div className="px-6 py-10 text-center">
                  <p className="text-sm text-text-secondary">
                    No rows to reconcile in this batch.
                  </p>
                </div>
              </div>
            ) : (
              <ul className="space-y-3">
                {rows.map((row) => (
                  <ReconcileRow
                    key={row.transaction_id}
                    row={row}
                    busy={rowState[row.transaction_id]?.busy ?? false}
                    onAction={(target) => {
                      // PR #247 P1: Edit and Match need extra payload
                      // (the modal collects it). The other targets
                      // commit immediately.
                      if (target === "edited") {
                        setEditModalRow(row);
                      } else if (target === "matched") {
                        setMatchModalRow(row);
                      } else {
                        applyTransition(row.transaction_id, target);
                      }
                    }}
                  />
                ))}
              </ul>
            )}
          </>
        ) : null}

        {/* ── Edit modal (PR #247 P1) ──────────────────────────────── */}
        {editModalRow ? (
          <EditModal
            row={editModalRow}
            onClose={() => setEditModalRow(null)}
            onSave={async (edits) => {
              const target = editModalRow.transaction_id;
              setEditModalRow(null);
              await applyTransition(target, "edited", { edits });
            }}
          />
        ) : null}

        {/* ── Match modal (PR #247 P1) ─────────────────────────────── */}
        {matchModalRow ? (
          <MatchModal
            row={matchModalRow}
            onClose={() => setMatchModalRow(null)}
            onSave={async (matchId) => {
              const target = matchModalRow.transaction_id;
              setMatchModalRow(null);
              await applyTransition(target, "matched", {
                match_with_transaction_id: matchId,
              });
            }}
          />
        ) : null}
      </div>
    </AppShell>
  );
}

// ── Edit modal ──────────────────────────────────────────────────────────────

function EditModal({
  row,
  onClose,
  onSave,
}: {
  row: ReconciliationRow;
  onClose: () => void;
  onSave: (edits: {
    description?: string;
    amount?: string;
    date?: string;
  }) => Promise<void>;
}) {
  const [description, setDescription] = useState(row.description);
  const [amount, setAmount] = useState(row.amount);
  const [date, setDate] = useState(row.date);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      // Only send fields the user actually changed. The backend reads
      // None as "leave alone", so omitting unchanged fields is the
      // right contract.
      const edits: { description?: string; amount?: string; date?: string } = {};
      if (description !== row.description) edits.description = description;
      if (amount !== row.amount) edits.amount = amount;
      if (date !== row.date) edits.date = date;
      if (Object.keys(edits).length === 0) {
        setErr("Make a change before saving, or close this dialog.");
        setBusy(false);
        return;
      }
      await onSave(edits);
    } catch (caught) {
      setErr(extractErrorMessage(caught, "Edit failed"));
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="recon-edit-title"
      data-testid="edit-modal"
    >
      <div className={`${card} w-full max-w-md`}>
        <div className={cardHeader}>
          <h2 id="recon-edit-title" className="text-base font-semibold text-text-primary">
            Edit imported row
          </h2>
        </div>
        <form onSubmit={handleSubmit} className="space-y-3 px-6 py-4">
          <div>
            <label htmlFor="recon-edit-description" className="mb-1 block text-xs text-text-muted">
              Description
            </label>
            <input
              id="recon-edit-description"
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary"
              data-testid="edit-description"
            />
          </div>
          <div>
            <label htmlFor="recon-edit-amount" className="mb-1 block text-xs text-text-muted">
              Amount
            </label>
            <input
              id="recon-edit-amount"
              type="text"
              inputMode="decimal"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              className="w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary"
              data-testid="edit-amount"
            />
          </div>
          <div>
            <label htmlFor="recon-edit-date" className="mb-1 block text-xs text-text-muted">
              Date
            </label>
            <input
              id="recon-edit-date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary"
              data-testid="edit-date"
            />
          </div>
          {err ? <p className={errorCls}>{err}</p> : null}
          <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className={`${btnPrimary} min-h-[44px] sm:min-h-0`}
              data-testid="edit-save"
            >
              {busy ? "Saving..." : "Save edits"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Match modal ─────────────────────────────────────────────────────────────

function MatchModal({
  row,
  onClose,
  onSave,
}: {
  row: ReconciliationRow;
  onClose: () => void;
  onSave: (matchId: number) => Promise<void>;
}) {
  const [matchId, setMatchId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    const parsed = Number(matchId);
    if (!Number.isInteger(parsed) || parsed <= 0) {
      setErr("Enter a valid transaction ID to match against.");
      setBusy(false);
      return;
    }
    if (parsed === row.transaction_id) {
      setErr("Match target must differ from this transaction.");
      setBusy(false);
      return;
    }
    try {
      await onSave(parsed);
    } catch (caught) {
      setErr(extractErrorMessage(caught, "Match failed"));
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="recon-match-title"
      data-testid="match-modal"
    >
      <div className={`${card} w-full max-w-md`}>
        <div className={cardHeader}>
          <h2 id="recon-match-title" className="text-base font-semibold text-text-primary">
            Match to existing transaction
          </h2>
        </div>
        <form onSubmit={handleSubmit} className="space-y-3 px-6 py-4">
          <p className="text-sm text-text-secondary">
            Enter the ID of the existing transaction this imported row should
            link to. The match is informational; both rows remain in the
            ledger.
          </p>
          <div>
            <label htmlFor="recon-match-id" className="mb-1 block text-xs text-text-muted">
              Existing transaction ID
            </label>
            <input
              id="recon-match-id"
              type="number"
              inputMode="numeric"
              min={1}
              value={matchId}
              onChange={(e) => setMatchId(e.target.value)}
              className="w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary"
              data-testid="match-id-input"
              placeholder="e.g. 1234"
            />
          </div>
          {err ? <p className={errorCls}>{err}</p> : null}
          <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className={`${btnPrimary} min-h-[44px] sm:min-h-0`}
              data-testid="match-save"
            >
              {busy ? "Saving..." : "Save match"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Per-row card ────────────────────────────────────────────────────────────

function ReconcileRow({
  row,
  busy,
  onAction,
}: {
  row: ReconciliationRow;
  busy: boolean;
  onAction: (target: ReconciliationState) => void;
}) {
  const nextStates = ALLOWED_NEXT[row.reconciliation_state] ?? [];

  return (
    <li
      className={`${card} relative px-4 py-3 sm:px-6`}
      data-testid="reconcile-row"
      data-state={row.reconciliation_state}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={STATE_BADGE[row.reconciliation_state]}>
              {STATE_LABEL[row.reconciliation_state]}
            </span>
            <span className="text-xs text-text-muted">
              {formatDate(row.date)}
            </span>
          </div>
          <p className="mt-2 break-words text-sm font-medium text-text-primary">
            {row.description}
          </p>
          <p
            className={
              row.type === "income"
                ? "mt-0.5 text-xs font-medium text-success"
                : "mt-0.5 text-xs font-medium text-text-secondary"
            }
          >
            {formatAmount(row.amount, row.type)}
          </p>
          {row.duplicate_warning ? (
            <div
              className={`${badgeWarning} mt-2`}
              data-testid="duplicate-warning"
              role="status"
            >
              Possible duplicate of transaction
              {row.duplicate_warning_target
                ? ` #${row.duplicate_warning_target}`
                : ""}
              . Review before accepting.
            </div>
          ) : null}
        </div>

        {/* Action cluster */}
        <div className="flex flex-shrink-0 flex-wrap items-center justify-end gap-2">
          {busy ? (
            <Spinner />
          ) : nextStates.length === 0 ? (
            <span className="text-xs italic text-text-muted">
              No further actions
            </span>
          ) : (
            nextStates.map((target) => {
              const klass =
                target === "accepted"
                  ? btnPrimary
                  : target === "rejected"
                    ? btnSecondary
                    : btnSecondary;
              return (
                <button
                  key={target}
                  type="button"
                  className={`${klass} min-h-[44px] text-xs sm:text-sm`}
                  onClick={() => onAction(target)}
                  data-testid={`action-${target}`}
                  aria-label={`${ACTION_LABEL[target]} row ${row.transaction_id}`}
                >
                  {ACTION_LABEL[target]}
                </button>
              );
            })
          )}
        </div>
      </div>
    </li>
  );
}
