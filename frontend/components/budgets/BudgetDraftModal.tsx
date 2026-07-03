"use client";

import { useEffect, useMemo, useState } from "react";

import Spinner from "@/components/ui/Spinner";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { btnPrimary, btnSecondary, card, error as errorCls } from "@/lib/styles";
import type { RebalanceSuggestion } from "@/components/budgets/BudgetRebalanceModal";

export type DraftStatus = "ok" | "empty_no_history";

export interface DraftResponse {
  status: DraftStatus;
  period_start: string | null;
  suggestions: RebalanceSuggestion[];
  summary: string;
}

interface Props {
  open: boolean;
  /** The next period's start_date; every created budget is scoped to it. */
  periodStart: string;
  /** Called after budgets are created so the parent can reload its list. */
  onApplied: () => void;
  onClose: () => void;
}

function toNumber(value: string | number): number {
  return typeof value === "string" ? Number(value) : value;
}

export default function BudgetDraftModal({
  open,
  periodStart,
  onApplied,
  onClose,
}: Props) {
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<DraftResponse | null>(null);
  const [acceptedIds, setAcceptedIds] = useState<Set<number>>(new Set());
  const [fetchError, setFetchError] = useState("");
  const [applyError, setApplyError] = useState("");
  const [applying, setApplying] = useState(false);
  const [createdIds, setCreatedIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setResponse(null);
    setFetchError("");
    setApplyError("");
    setAcceptedIds(new Set());
    setCreatedIds(new Set());
    apiFetch<DraftResponse>(
      `/api/v1/budgets/draft-next?period_start=${periodStart}`,
      { method: "POST" },
    )
      .then((res) => {
        if (cancelled) return;
        if (res) {
          setResponse(res);
          // Accept-by-default (opt-out per row), mirroring the rebalance UX.
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
  }, [open, periodStart]);

  const hasSuggestions =
    response?.status === "ok" && (response.suggestions?.length ?? 0) > 0;

  const acceptedCount = acceptedIds.size;

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
    const created = new Set<number>(createdIds);
    const pending = response.suggestions.filter(
      (s) => acceptedIds.has(s.category_id) && !created.has(s.category_id),
    );
    let firstError: unknown = null;
    for (const s of pending) {
      try {
        await apiFetch(`/api/v1/budgets?period_start=${periodStart}`, {
          method: "POST",
          body: JSON.stringify({
            category_id: s.category_id,
            amount: toNumber(s.suggested_amount),
          }),
        });
        created.add(s.category_id);
      } catch (err) {
        if (firstError === null) firstError = err;
        // Stop on first failure so the user can see exactly what landed.
        break;
      }
    }
    setCreatedIds(created);
    // Uncheck rows that already landed so a retry only targets the rest.
    setAcceptedIds((prev) => {
      const next = new Set(prev);
      for (const id of created) next.delete(id);
      return next;
    });
    setApplying(false);
    const landedThisAttempt = created.size > createdIds.size;
    if (landedThisAttempt) onApplied();
    if (firstError !== null) {
      setApplyError(extractErrorMessage(firstError));
    } else if (pending.every((s) => created.has(s.category_id))) {
      onClose();
    }
  }

  const totalDraft = useMemo(
    () =>
      response
        ? response.suggestions
            .filter((s) => acceptedIds.has(s.category_id))
            .reduce((acc, s) => acc + toNumber(s.suggested_amount), 0)
        : 0,
    [response, acceptedIds],
  );

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="draft-title"
    >
      <div className={`${card} relative w-full max-w-3xl max-h-[90vh] overflow-y-auto`}>
        <div className="flex items-start justify-between border-b border-border-subtle px-6 py-4">
          <div>
            <h2 id="draft-title" className="text-base font-semibold text-text-primary">
              Draft next period from trends
            </h2>
            <p className="mt-1 text-xs text-text-muted">
              Projected from your last 3 months of spending. Nothing is saved
              until you click Apply.
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

          {!loading && response && !hasSuggestions && (
            <div className="py-10 text-center" data-testid="draft-empty-state">
              <p className="text-sm font-medium text-text-primary">
                Nothing to draft yet
              </p>
              <p className="mt-2 text-xs text-text-muted">
                {response.summary ||
                  "Not enough recent spending history to draft a budget."}
              </p>
            </div>
          )}

          {!loading && response && hasSuggestions && (
            <>
              {response.summary && (
                <p className="mb-4 text-sm text-text-secondary">
                  {response.summary}
                </p>
              )}
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="draft-table">
                  <thead>
                    <tr className="border-b border-border-subtle text-left">
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Add
                      </th>
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Category
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Suggested
                      </th>
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Basis
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {response.suggestions.map((s) => {
                      const accepted = acceptedIds.has(s.category_id);
                      const wasCreated = createdIds.has(s.category_id);
                      return (
                        <tr
                          key={s.category_id}
                          className="border-b border-border-subtle/60 align-top"
                          data-testid={`draft-row-${s.category_id}`}
                        >
                          <td className="py-2 pr-3">
                            <input
                              type="checkbox"
                              aria-label={`Add budget for ${s.category_name}`}
                              checked={accepted}
                              onChange={() => toggleRow(s.category_id)}
                              disabled={wasCreated}
                              className="h-4 w-4 accent-accent"
                            />
                          </td>
                          <td className="py-2 pr-3 text-text-primary">
                            {s.category_name}
                            {wasCreated && (
                              <span className="ml-2 text-[10px] uppercase tracking-wide text-success">
                                added
                              </span>
                            )}
                          </td>
                          <td className="py-2 pr-3 text-right tabular-nums text-text-primary">
                            {formatAmount(toNumber(s.suggested_amount))}
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
                {acceptedCount} of {response.suggestions.length} selected. Total
                drafted: {formatAmount(totalDraft)}.
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
          {hasSuggestions && (
            <button
              type="button"
              onClick={handleApply}
              disabled={applying || acceptedCount === 0}
              className={btnPrimary}
            >
              {applying
                ? "Adding..."
                : `Apply ${acceptedCount} budget${acceptedCount === 1 ? "" : "s"}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
