"use client";

import { useCallback, useMemo, useState } from "react";
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
  pending_review: ["accepted", "skipped", "rejected"],
  unmatched: ["accepted", "skipped", "rejected"],
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
                    className="h-full bg-accent transition-all"
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
                    onAction={(target) =>
                      applyTransition(row.transaction_id, target)
                    }
                  />
                ))}
              </ul>
            )}
          </>
        ) : null}
      </div>
    </AppShell>
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
