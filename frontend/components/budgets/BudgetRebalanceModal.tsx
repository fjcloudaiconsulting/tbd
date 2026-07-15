"use client";

import { useEffect, useMemo, useState } from "react";

import Spinner from "@/components/ui/Spinner";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { btnPrimary, btnSecondary, card, error as errorCls } from "@/lib/styles";

export type RebalanceStatus =
  | "ok"
  | "empty_no_budgets"
  | "empty_no_history"
  | "empty_no_surplus"
  | "llm_unavailable";

export interface RebalanceSuggestion {
  category_id: number;
  category_name: string;
  current_amount: string | number;
  suggested_amount: string | number;
  delta_amount: string | number;
  reasoning: string;
}

export interface RebalanceResponse {
  status: RebalanceStatus;
  period_start: string | null;
  suggestions: RebalanceSuggestion[];
  summary: string;
  // Conservation fields (zero-sum rebalance). Optional so older payloads
  // still type-check; the meter/banner default to a balanced reading.
  total_budget?: string | number;
  total_suggested?: string | number;
  uncovered_overspend?: string | number;
  is_balanced?: boolean;
}

interface Budget {
  id: number;
  category_id: number;
  amount: string | number;
}

interface Props {
  open: boolean;
  budgets: Budget[];
  /** Called after the user clicks Apply and the writes succeed.
   *  Lets the parent reload its budgets list. */
  onApplied: () => void;
  onClose: () => void;
}

function toNumber(value: string | number): number {
  return typeof value === "string" ? Number(value) : value;
}

export default function BudgetRebalanceModal({
  open,
  budgets,
  onApplied,
  onClose,
}: Props) {
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<RebalanceResponse | null>(null);
  const [acceptedIds, setAcceptedIds] = useState<Set<number>>(new Set());
  const [fetchError, setFetchError] = useState<string>("");
  const [applyError, setApplyError] = useState<string>("");
  const [applying, setApplying] = useState(false);
  // Per-row apply state: which suggestions have been written successfully
  // this apply attempt, and which (if any) failed. Lets the user see
  // exactly what landed when a mid-loop failure cuts the loop short.
  const [appliedIds, setAppliedIds] = useState<Set<number>>(new Set());
  const [failedIds, setFailedIds] = useState<Set<number>>(new Set());
  const [skippedIds, setSkippedIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset the modal's loading/response/error/selection state each time it opens, before fetching the rebalance
    setLoading(true);
    setResponse(null);
    setFetchError("");
    setApplyError("");
    setAcceptedIds(new Set());
    setAppliedIds(new Set());
    setFailedIds(new Set());
    setSkippedIds(new Set());
    apiFetch<RebalanceResponse>("/api/v1/ai/budget/rebalance", {
      method: "POST",
    })
      .then((res) => {
        if (cancelled) return;
        if (res) {
          setResponse(res);
          // Accept-by-default: each suggestion is opt-out, mirroring
          // the diff-review UX. The user can skip individual rows
          // before clicking Apply.
          setAcceptedIds(
            new Set((res.suggestions ?? []).map((s) => s.category_id)),
          );
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setFetchError(extractErrorMessage(err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const budgetIdByCategory = useMemo(() => {
    const m = new Map<number, number>();
    for (const b of budgets) m.set(b.category_id, b.id);
    return m;
  }, [budgets]);

  function toggleRow(categoryId: number) {
    setAcceptedIds((prev) => {
      const next = new Set(prev);
      if (next.has(categoryId)) next.delete(categoryId);
      else next.add(categoryId);
      return next;
    });
  }

  async function handleApply() {
    if (!response) return;
    setApplyError("");
    setApplying(true);
    // Carry forward prior-attempt results so a retry only targets
    // rows that still need to land — without this, re-clicking Apply
    // after a partial failure would re-PUT the rows that ALREADY
    // applied (charging the user's budget twice in spirit, even
    // though the API call is idempotent).
    const cumulativeApplied = new Set<number>(appliedIds);
    const cumulativeFailed = new Set<number>();
    const cumulativeSkipped = new Set<number>();
    const pending = response.suggestions.filter(
      (s) =>
        acceptedIds.has(s.category_id) && !cumulativeApplied.has(s.category_id),
    );
    let firstError: unknown = null;
    let abortedAtIndex = -1;
    for (let i = 0; i < pending.length; i++) {
      const s = pending[i];
      const budgetId = budgetIdByCategory.get(s.category_id);
      if (!budgetId) {
        // The user's budgets prop drifted (a budget was deleted between
        // open and apply). Surface it instead of silently dropping.
        cumulativeSkipped.add(s.category_id);
        continue;
      }
      try {
        await apiFetch(`/api/v1/budgets/${budgetId}`, {
          method: "PUT",
          body: JSON.stringify({ amount: toNumber(s.suggested_amount) }),
        });
        cumulativeApplied.add(s.category_id);
      } catch (err) {
        cumulativeFailed.add(s.category_id);
        if (firstError === null) firstError = err;
        abortedAtIndex = i;
        // Stop on first failure — applying further rows after a
        // server-side rejection would mask the failure and make it
        // harder for the user to recover.
        break;
      }
    }
    // Anything we didn't even attempt because of the break above
    // shouldn't keep its checkbox checked — otherwise the next Apply
    // would happily retry them all. Mark them as 'skipped' so the
    // user can re-check explicitly if they want to try again.
    if (abortedAtIndex >= 0) {
      for (let i = abortedAtIndex + 1; i < pending.length; i++) {
        cumulativeSkipped.add(pending[i].category_id);
      }
    }
    setAppliedIds(cumulativeApplied);
    setFailedIds(cumulativeFailed);
    setSkippedIds(cumulativeSkipped);
    // Uncheck rows that already applied so the count + sum line and
    // the Apply-button label reflect only the work still to do.
    setAcceptedIds((prev) => {
      const next = new Set(prev);
      for (const id of cumulativeApplied) next.delete(id);
      return next;
    });
    if (firstError !== null) {
      setApplyError(extractErrorMessage(firstError));
    } else if (
      cumulativeSkipped.size > 0 &&
      cumulativeApplied.size === appliedIds.size
    ) {
      setApplyError(
        "No budget rows could be applied. Refresh and try again.",
      );
    }
    setApplying(false);
    // Notify the parent if anything landed THIS attempt.
    const landedThisAttempt =
      cumulativeApplied.size > appliedIds.size;
    if (landedThisAttempt) {
      onApplied();
    }
    // Auto-close only when every accepted row has now landed and
    // nothing failed/was skipped this attempt.
    if (
      cumulativeFailed.size === 0 &&
      cumulativeSkipped.size === 0 &&
      pending.every((s) => cumulativeApplied.has(s.category_id))
    ) {
      onClose();
    }
  }

  if (!open) return null;

  const hasOkSuggestions =
    response?.status === "ok" && (response.suggestions?.length ?? 0) > 0;

  const acceptedCount = acceptedIds.size;
  const acceptedSum = response
    ? response.suggestions
        .filter((s) => acceptedIds.has(s.category_id))
        .reduce((acc, s) => acc + toNumber(s.delta_amount), 0)
    : 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rebalance-title"
    >
      <div
        className={`${card} relative w-full max-w-3xl max-h-[90vh] overflow-y-auto`}
      >
        <div className="flex items-start justify-between border-b border-border-subtle px-6 py-4">
          <div>
            <h2
              id="rebalance-title"
              className="text-base font-semibold text-text-primary"
            >
              AI budget rebalance
            </h2>
            <p className="mt-1 text-xs text-text-muted">
              Suggestions only. Nothing is applied until you click Apply.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-text-muted hover:text-text-primary"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="px-6 py-5">
          {loading && (
            <div className="flex justify-center py-12">
              <Spinner />
            </div>
          )}

          {!loading && fetchError && (
            <div className={`mb-4 ${errorCls}`} role="alert">
              {fetchError}
            </div>
          )}

          {!loading && response && response.status !== "ok" && (
            <EmptyState status={response.status} message={response.summary} />
          )}

          {!loading && response && response.status === "ok" && !hasOkSuggestions && (
            <EmptyState
              status="ok_no_changes"
              message={
                response.summary ||
                "AI looked at your budgets and didn't recommend any changes."
              }
            />
          )}

          {!loading && response && hasOkSuggestions && (
            <>
              {response.summary && (
                <p className="mb-4 text-sm text-text-secondary">
                  {response.summary}
                </p>
              )}
              {Number(response.uncovered_overspend ?? 0) > 0 && (
                <div
                  data-testid="rebalance-uncovered"
                  className="mb-4 rounded-md bg-warning-dim px-3 py-2 text-xs text-warning"
                  role="status"
                >
                  You&apos;re {formatAmount(Number(response.uncovered_overspend))}{" "}
                  over plan this period. Spending exceeds your total budget, so
                  not every category could be fully covered.
                </div>
              )}
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="rebalance-diff-table">
                  <thead>
                    <tr className="border-b border-border-subtle text-left">
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Apply
                      </th>
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Category
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Current
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Suggested
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Delta
                      </th>
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Reasoning
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {response.suggestions.map((s) => {
                      const delta = toNumber(s.delta_amount);
                      const accepted = acceptedIds.has(s.category_id);
                      const wasApplied = appliedIds.has(s.category_id);
                      const wasFailed = failedIds.has(s.category_id);
                      const wasSkipped = skippedIds.has(s.category_id);
                      const rowStatus = wasApplied
                        ? "applied"
                        : wasFailed
                          ? "failed"
                          : wasSkipped
                            ? "skipped"
                            : null;
                      return (
                        <tr
                          key={s.category_id}
                          className="border-b border-border-subtle/60 align-top"
                          data-testid={`rebalance-row-${s.category_id}`}
                          data-row-status={rowStatus ?? "pending"}
                        >
                          <td className="py-2 pr-3">
                            <input
                              type="checkbox"
                              aria-label={`Apply suggestion for ${s.category_name}`}
                              checked={accepted}
                              onChange={() => toggleRow(s.category_id)}
                              disabled={wasApplied}
                              className="h-4 w-4 accent-accent"
                            />
                          </td>
                          <td className="py-2 pr-3 text-text-primary">
                            {s.category_name}
                            {rowStatus && (
                              <span
                                className={`ml-2 text-[10px] uppercase tracking-wide ${
                                  wasApplied
                                    ? "text-success"
                                    : wasFailed
                                      ? "text-danger"
                                      : "text-text-muted"
                                }`}
                                data-testid={`rebalance-row-${s.category_id}-status`}
                              >
                                {rowStatus}
                              </span>
                            )}
                          </td>
                          <td className="py-2 pr-3 text-right tabular-nums text-text-secondary">
                            {formatAmount(toNumber(s.current_amount))}
                          </td>
                          <td className="py-2 pr-3 text-right tabular-nums text-text-primary">
                            {formatAmount(toNumber(s.suggested_amount))}
                          </td>
                          <td
                            className={`py-2 pr-3 text-right tabular-nums ${
                              delta > 0
                                ? "text-success"
                                : delta < 0
                                  ? "text-danger"
                                  : "text-text-muted"
                            }`}
                          >
                            {delta > 0 ? "+" : ""}
                            {formatAmount(delta)}
                          </td>
                          <td className="py-2 pr-3 text-xs text-text-muted">
                            {s.reasoning}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div
                data-testid="rebalance-balance-meter"
                className={`mt-3 rounded-md px-3 py-2 text-xs ${
                  Math.abs(acceptedSum) < 0.005
                    ? "bg-surface-raised/40 text-text-muted"
                    : "bg-warning-dim text-warning"
                }`}
              >
                {acceptedCount} of {response.suggestions.length} changes
                selected.{" "}
                {Math.abs(acceptedSum) < 0.005 ? (
                  <>Net change: {formatAmount(0)}. Balanced.</>
                ) : (
                  <>
                    This changes your total budget by{" "}
                    {acceptedSum > 0 ? "+" : ""}
                    {formatAmount(acceptedSum)}.
                  </>
                )}
              </div>
            </>
          )}

          {applyError && (
            <div className={`mt-4 ${errorCls}`} role="alert">
              {applyError}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border-subtle bg-surface-raised/30 px-6 py-3">
          <button type="button" onClick={onClose} className={btnSecondary}>
            Cancel
          </button>
          {hasOkSuggestions && (
            <button
              type="button"
              onClick={handleApply}
              disabled={applying || acceptedCount === 0}
              className={btnPrimary}
            >
              {applying ? "Applying..." : `Apply ${acceptedCount} change${acceptedCount === 1 ? "" : "s"}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function EmptyState({
  status,
  message,
}: {
  status: RebalanceStatus | "ok_no_changes";
  message: string;
}) {
  const title =
    status === "empty_no_budgets"
      ? "No budgets yet"
      : status === "empty_no_history"
        ? "Not enough history yet"
        : status === "empty_no_surplus"
          ? "Nothing to reallocate"
          : status === "llm_unavailable"
            ? "AI is unavailable"
            : "Nothing to rebalance";
  return (
    <div className="py-10 text-center" data-testid="rebalance-empty-state">
      <p className="text-sm font-medium text-text-primary">{title}</p>
      <p className="mt-2 text-xs text-text-muted">{message}</p>
    </div>
  );
}
