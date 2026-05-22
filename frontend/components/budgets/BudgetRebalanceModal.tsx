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

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setResponse(null);
    setFetchError("");
    setApplyError("");
    setAcceptedIds(new Set());
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
    try {
      const accepted = response.suggestions.filter((s) =>
        acceptedIds.has(s.category_id),
      );
      for (const s of accepted) {
        const budgetId = budgetIdByCategory.get(s.category_id);
        if (!budgetId) continue;
        await apiFetch(`/api/v1/budgets/${budgetId}`, {
          method: "PUT",
          body: JSON.stringify({ amount: toNumber(s.suggested_amount) }),
        });
      }
      onApplied();
      onClose();
    } catch (err) {
      setApplyError(extractErrorMessage(err));
    } finally {
      setApplying(false);
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
                      return (
                        <tr
                          key={s.category_id}
                          className="border-b border-border-subtle/60 align-top"
                          data-testid={`rebalance-row-${s.category_id}`}
                        >
                          <td className="py-2 pr-3">
                            <input
                              type="checkbox"
                              aria-label={`Apply suggestion for ${s.category_name}`}
                              checked={accepted}
                              onChange={() => toggleRow(s.category_id)}
                              className="h-4 w-4 accent-accent"
                            />
                          </td>
                          <td className="py-2 pr-3 text-text-primary">
                            {s.category_name}
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
              <p className="mt-3 text-xs text-text-muted">
                {acceptedCount} of {response.suggestions.length} changes
                selected. Net change: {acceptedSum > 0 ? "+" : ""}
                {formatAmount(acceptedSum)}.
              </p>
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
