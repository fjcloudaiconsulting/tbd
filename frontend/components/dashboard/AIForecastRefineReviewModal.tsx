"use client";

import { useEffect, useMemo, useState } from "react";

import type { RefinedForecastResponse } from "@/components/dashboard/AIForecastRefineToggle";
import { formatAmount } from "@/lib/format";
import { btnPrimary, btnSecondary, card } from "@/lib/styles";

/**
 * Per-row review step for AI forecast refinement.
 *
 * Mirrors the BudgetRebalanceModal accept/skip diff pattern so the AI
 * features stay consistent: the AI returns suggested per-category
 * adjustments, the user reviews each one and accepts/skips it, and
 * NOTHING is reflected on the forecast until they click Apply. Unlike
 * the budget rebalance (which PUTs each accepted row), forecast refine
 * is display-only, so "Apply" simply tells the parent which category ids
 * the user accepted. The parent recomputes the displayed refined
 * forecast from the accepted subset (skipped categories fall back to
 * their baseline).
 */

export interface AIForecastRefineReviewModalProps {
  open: boolean;
  refined: RefinedForecastResponse;
  /** Called with the set of accepted category ids when the user clicks Apply. */
  onApply: (acceptedCategoryIds: Set<number>) => void;
  onClose: () => void;
}

export default function AIForecastRefineReviewModal({
  open,
  refined,
  onApply,
  onClose,
}: AIForecastRefineReviewModalProps) {
  // Only categories the AI actually adjusted (multiplier != 1) are
  // reviewable; unchanged categories carry the baseline regardless.
  const adjustments = useMemo(
    () => refined.categories.filter((c) => c.multiplier !== 1),
    [refined.categories],
  );

  const [acceptedIds, setAcceptedIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (!open) return;
    // Accept-by-default: each adjustment is opt-out, matching the
    // rebalance diff-review UX. The user can skip individual rows
    // before clicking Apply.
    setAcceptedIds(new Set(adjustments.map((a) => a.category_id)));
  }, [open, adjustments]);

  function toggleRow(categoryId: number) {
    setAcceptedIds((prev) => {
      const next = new Set(prev);
      if (next.has(categoryId)) next.delete(categoryId);
      else next.add(categoryId);
      return next;
    });
  }

  if (!open) return null;

  const acceptedCount = acceptedIds.size;
  // Net displayed delta from the accepted subset (skipped rows contribute
  // zero, since they keep their baseline).
  const acceptedDelta = adjustments
    .filter((a) => acceptedIds.has(a.category_id))
    .reduce(
      (acc, a) => acc + (Number(a.refined_forecast) - Number(a.baseline_forecast)),
      0,
    );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="forecast-refine-review-title"
    >
      <div
        className={`${card} relative w-full max-w-3xl max-h-[90vh] overflow-y-auto`}
      >
        <div className="flex items-start justify-between border-b border-border-subtle px-6 py-4">
          <div>
            <h2
              id="forecast-refine-review-title"
              className="text-base font-semibold text-text-primary"
            >
              Review AI forecast adjustments
            </h2>
            <p className="mt-1 text-xs text-text-muted">
              Suggestions only. Nothing changes on your forecast until you click
              Apply.
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
          {refined.provenance.summary && (
            <p className="mb-4 text-sm text-text-secondary">
              {refined.provenance.summary}
            </p>
          )}

          {adjustments.length === 0 ? (
            <div
              className="py-10 text-center"
              data-testid="forecast-refine-review-empty"
            >
              <p className="text-sm font-medium text-text-primary">
                No adjustments to review
              </p>
              <p className="mt-2 text-xs text-text-muted">
                AI looked at your forecast and did not recommend any category
                changes.
              </p>
            </div>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table
                  className="w-full text-sm"
                  data-testid="forecast-refine-diff-table"
                >
                  <thead>
                    <tr className="border-b border-border-subtle text-left">
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Apply
                      </th>
                      <th className="py-2 pr-3 text-xs font-medium text-text-muted">
                        Category
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Baseline
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Refined
                      </th>
                      <th className="py-2 pr-3 text-right text-xs font-medium text-text-muted">
                        Delta
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {adjustments.map((a) => {
                      const baseline = Number(a.baseline_forecast);
                      const refinedAmt = Number(a.refined_forecast);
                      const delta = refinedAmt - baseline;
                      const accepted = acceptedIds.has(a.category_id);
                      return (
                        <tr
                          key={a.category_id}
                          className="border-b border-border-subtle/60 align-top"
                          data-testid={`forecast-refine-row-${a.category_id}`}
                          data-row-accepted={accepted ? "yes" : "no"}
                        >
                          <td className="py-2 pr-3">
                            <input
                              type="checkbox"
                              aria-label={`Apply adjustment for ${a.category_name}`}
                              checked={accepted}
                              onChange={() => toggleRow(a.category_id)}
                              className="h-4 w-4 accent-accent"
                            />
                          </td>
                          <td className="py-2 pr-3 text-text-primary">
                            {a.category_name}
                            <span className="ml-2 text-[10px] uppercase tracking-wide text-text-muted">
                              x{a.multiplier.toFixed(2)}
                            </span>
                          </td>
                          <td className="py-2 pr-3 text-right tabular-nums text-text-secondary">
                            {formatAmount(baseline)}
                          </td>
                          <td className="py-2 pr-3 text-right tabular-nums text-text-primary">
                            {formatAmount(refinedAmt)}
                          </td>
                          <td
                            className={`py-2 pr-3 text-right tabular-nums ${
                              delta > 0
                                ? "text-danger"
                                : delta < 0
                                  ? "text-success"
                                  : "text-text-muted"
                            }`}
                          >
                            {delta > 0 ? "+" : ""}
                            {formatAmount(delta)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs text-text-muted">
                {acceptedCount} of {adjustments.length} adjustments selected.
                Net change: {acceptedDelta > 0 ? "+" : ""}
                {formatAmount(acceptedDelta)}.
              </p>
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border-subtle bg-surface-raised/30 px-6 py-3">
          <button type="button" onClick={onClose} className={btnSecondary}>
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onApply(acceptedIds)}
            className={btnPrimary}
            data-testid="forecast-refine-apply"
          >
            {adjustments.length === 0
              ? "Done"
              : `Apply ${acceptedCount} adjustment${acceptedCount === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
