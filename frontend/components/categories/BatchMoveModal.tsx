"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, ApiResponseError, extractErrorMessage } from "@/lib/api";
import { btnPrimary, btnSecondary, input } from "@/lib/styles";
import type { Category } from "@/lib/types";

interface CategoryMoveResult {
  category_id: number;
  source_master_id: number;
  target_master_id: number;
  affected_transaction_count: number;
  affected_recurring_count: number;
  affected_forecast_item_count: number;
  budget_actuals_shifted: boolean;
}

interface BatchMoveResultBody {
  moves: CategoryMoveResult[];
}

interface PreviewAggregate {
  transactions: number;
  recurring: number;
  forecast: number;
  budget_actuals_shifted: boolean;
}

interface Props {
  open: boolean;
  selectedIds: number[];
  categories: Category[];
  onCancel: () => void;
  onSuccess: () => void;
}

/**
 * Two-step batch-move flow:
 * 1. Pick a target master (filterable list of compatible masters).
 * 2. Confirm against aggregate preview counts and call the all-or-nothing
 *    POST /api/v1/categories/batch-move endpoint per the C0 spec section 3.C.
 *
 * Compatibility filter: every selected subcategory's type must be allowed by
 * the target master. INCOME source -> INCOME or BOTH target. EXPENSE source ->
 * EXPENSE or BOTH target. BOTH source -> BOTH target only (the safe default
 * matching spec section 4.6 frontend behaviour). Mixed selections fall back
 * to BOTH targets only.
 *
 * Owned by Team Categories C2 UI.
 */
export default function BatchMoveModal({
  open,
  selectedIds,
  categories,
  onCancel,
  onSuccess,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [filter, setFilter] = useState("");
  const [targetId, setTargetId] = useState<number | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState<PreviewAggregate | null>(null);
  const [previewError, setPreviewError] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string>("");

  const selectedSubs = useMemo(
    () => categories.filter((c) => selectedIds.includes(c.id) && c.parent_id !== null),
    [categories, selectedIds],
  );

  const requiredTargetTypes = useMemo<Array<Category["type"]>>(() => {
    const types = new Set(selectedSubs.map((s) => s.type));
    if (types.size === 0) return ["both"];
    if (types.size > 1) return ["both"];
    const sole = [...types][0];
    if (sole === "income") return ["income", "both"];
    if (sole === "expense") return ["expense", "both"];
    return ["both"];
  }, [selectedSubs]);

  const candidateMasters = useMemo(() => {
    const sq = filter.trim().toLowerCase();
    return categories
      .filter((c) => c.parent_id === null)
      .filter((c) => requiredTargetTypes.includes(c.type))
      .filter((m) => (sq ? m.name.toLowerCase().includes(sq) : true));
  }, [categories, filter, requiredTargetTypes]);

  // Reset internal state when the modal closes or the selection changes.
  useEffect(() => {
    if (!open) {
      setFilter("");
      setTargetId(null);
      setPreview(null);
      setPreviewError("");
      setSubmitError("");
      setSubmitting(false);
      setPreviewing(false);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onCancel]);

  useEffect(() => {
    if (!open) return;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  // Aggregate preview: call preview per subcategory, sum counts, OR the
  // budget_actuals_shifted flag.
  useEffect(() => {
    if (!open || targetId === null) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    let cancelled = false;
    async function loadPreview() {
      setPreviewing(true);
      setPreview(null);
      setPreviewError("");
      try {
        const results = await Promise.all(
          selectedSubs.map((sub) =>
            apiFetch<CategoryMoveResult>(
              `/api/v1/categories/${sub.id}/move/preview?target_parent_id=${targetId}`,
            ).catch((err) => {
              throw err;
            }),
          ),
        );
        if (cancelled) return;
        const agg: PreviewAggregate = {
          transactions: 0,
          recurring: 0,
          forecast: 0,
          budget_actuals_shifted: false,
        };
        for (const r of results) {
          agg.transactions += r.affected_transaction_count;
          agg.recurring += r.affected_recurring_count;
          agg.forecast += r.affected_forecast_item_count;
          agg.budget_actuals_shifted =
            agg.budget_actuals_shifted || r.budget_actuals_shifted;
        }
        setPreview(agg);
      } catch (err) {
        if (cancelled) return;
        setPreviewError(extractErrorMessage(err, "Could not load preview"));
      } finally {
        if (!cancelled) setPreviewing(false);
      }
    }
    void loadPreview();
    return () => {
      cancelled = true;
    };
  }, [open, targetId, selectedSubs]);

  async function handleConfirm() {
    if (targetId === null) return;
    setSubmitting(true);
    setSubmitError("");
    try {
      await apiFetch<BatchMoveResultBody>("/api/v1/categories/batch-move", {
        method: "POST",
        body: JSON.stringify({
          moves: selectedSubs.map((s) => ({
            subcategory_id: s.id,
            target_parent_id: targetId,
          })),
        }),
      });
      onSuccess();
    } catch (err) {
      // The C0 spec is all-or-nothing: a 4xx fails the whole batch. We surface
      // the structured detail (name_collision, type_mismatch, etc.) verbatim;
      // the user fixes the offending row and retries with the same target.
      if (err instanceof ApiResponseError && err.detail) {
        setSubmitError(buildBatchErrorMessage(err, selectedSubs));
      } else {
        setSubmitError(extractErrorMessage(err, "Move failed"));
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="batch-move-title"
        data-testid="batch-move-modal"
        className="w-full max-w-[min(32rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="batch-move-title" className="text-lg font-semibold text-text-primary">
          Move {selectedSubs.length} subcategor{selectedSubs.length === 1 ? "y" : "ies"}
        </h3>
        <p className="mt-1 text-xs text-text-muted">
          Pick a target master. All-or-nothing: if any move would collide or be type-incompatible, the whole batch is rejected.
        </p>

        <div className="mt-4">
          <label htmlFor="batch-move-filter" className="sr-only">
            Filter masters
          </label>
          <input
            id="batch-move-filter"
            type="text"
            placeholder="Filter masters..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className={input}
          />
        </div>

        <div
          role="radiogroup"
          aria-label="Target master"
          className="mt-3 max-h-56 overflow-y-auto rounded-md border border-border"
        >
          {candidateMasters.length === 0 ? (
            <p className="p-4 text-xs text-text-muted">
              No compatible masters. Selected subcategories require a target of type{" "}
              {requiredTargetTypes.join(" or ")}.
            </p>
          ) : (
            candidateMasters.map((m) => (
              <label
                key={m.id}
                data-testid={`batch-move-target-${m.id}`}
                className={`flex cursor-pointer items-center justify-between gap-3 border-b border-border px-3 py-2 last:border-b-0 hover:bg-surface-raised ${
                  targetId === m.id ? "bg-surface-raised" : ""
                }`}
              >
                <div className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="batch-move-target"
                    value={m.id}
                    checked={targetId === m.id}
                    onChange={() => setTargetId(m.id)}
                    className="h-4 w-4"
                  />
                  <span className="text-sm text-text-primary">{m.name}</span>
                </div>
                <span className="text-[11px] text-text-muted">{m.type}</span>
              </label>
            ))
          )}
        </div>

        {targetId !== null && (
          <div
            className="mt-4 rounded-md border border-border bg-surface-raised p-3 text-sm text-text-secondary"
            data-testid="batch-move-preview"
          >
            {previewing ? (
              <span>Loading preview...</span>
            ) : previewError ? (
              <span className="text-danger">{previewError}</span>
            ) : preview ? (
              <>
                <p>
                  Reassigns <strong>{preview.transactions}</strong> transaction
                  {preview.transactions === 1 ? "" : "s"},{" "}
                  <strong>{preview.recurring}</strong> recurring template
                  {preview.recurring === 1 ? "" : "s"}, and{" "}
                  <strong>{preview.forecast}</strong> forecast plan item
                  {preview.forecast === 1 ? "" : "s"}.
                </p>
                {preview.budget_actuals_shifted && (
                  <p className="mt-1 text-xs text-text-muted">
                    Current-period budget actuals will shift attribution. Planned amounts are unchanged.
                  </p>
                )}
              </>
            ) : null}
          </div>
        )}

        {submitError && (
          <div
            data-testid="batch-move-error"
            className="mt-4 whitespace-pre-line rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"
          >
            {submitError}
          </div>
        )}

        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onCancel}
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px]`}
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="batch-move-confirm"
            onClick={handleConfirm}
            disabled={targetId === null || submitting || previewing}
            className={`${btnPrimary} w-full sm:w-auto min-h-[44px]`}
          >
            {submitting ? "Moving..." : "Move"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface CategoryErrorDetail {
  detail?: string;
  conflicting_child_name?: string;
  target_parent_id?: number;
  source_type?: string;
  target_type?: string;
  dependent_breakdown?: { income: number; expense: number };
}

function buildBatchErrorMessage(err: ApiResponseError, subs: Category[]): string {
  const detail = err.detail as CategoryErrorDetail | string | undefined;
  if (typeof detail === "object" && detail !== null) {
    if (detail.detail === "name_collision" && detail.conflicting_child_name) {
      return `Target master already has a subcategory named "${detail.conflicting_child_name}". Rename one before moving.`;
    }
    if (detail.detail === "type_mismatch") {
      const breakdown = detail.dependent_breakdown
        ? ` (${detail.dependent_breakdown.income} income, ${detail.dependent_breakdown.expense} expense)`
        : "";
      return `Type mismatch${breakdown}. Pick a target compatible with the selected subcategories.`;
    }
  }
  return err.message || `Move failed for ${subs.length} subcategor${subs.length === 1 ? "y" : "ies"}.`;
}
