"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, ApiResponseError, extractErrorMessage } from "@/lib/api";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
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
  /** May be async; the modal awaits it and surfaces refresh errors
   *  inline with a Retry control. Mirrors the AddMasterWithSubsModal
   *  pattern so the post-mutation reload never silently drops a
   *  failure. */
  onSuccess: () => void | Promise<void>;
}

/**
 * Two-step batch-move flow:
 * 1. Pick a target master (filterable list of compatible masters).
 * 2. Confirm against aggregate preview counts and call the all-or-nothing
 *    POST /api/v1/categories/batch-move endpoint per the C0 spec section 3.C.
 *
 * Compatibility filter mirrors the backend rule at
 * `category_service.move_subcategory` line ~754: `sub.type == target.type`
 * (strict equality). INCOME source -> INCOME target only. EXPENSE source ->
 * EXPENSE target only. BOTH source -> BOTH target only. Mixed selections
 * are blocked at the picker because the user must move one type at a time.
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
  const filterRef = useRef<HTMLInputElement>(null);
  const [filter, setFilter] = useState("");
  const [targetId, setTargetId] = useState<number | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState<PreviewAggregate | null>(null);
  const [previewError, setPreviewError] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string>("");
  const [refreshError, setRefreshError] = useState<string>("");

  const selectedSubs = useMemo(
    () => categories.filter((c) => selectedIds.includes(c.id) && c.parent_id !== null),
    [categories, selectedIds],
  );

  // Mixed selections (income + expense in the same batch) cannot share a
  // target master under the strict-equality rule. The picker hides all
  // candidates and surfaces an inline message asking the user to move
  // one type at a time. Submit stays disabled until the selection is
  // single-typed.
  const selectedTypes = useMemo(
    () => new Set(selectedSubs.map((s) => s.type)),
    [selectedSubs],
  );
  const mixedSelection = selectedTypes.size > 1;
  const soleType = selectedTypes.size === 1
    ? ([...selectedTypes][0] as Category["type"])
    : null;

  // Source masters of the current selection. Backend rejects a move where
  // `target_parent_id == sub.parent_id` as a no-op
  // (`category_service.move_subcategory` line 748). We strip these IDs from
  // the candidate list so the UI never offers a target that would 400. When
  // the selection spans multiple masters, ALL source masters are excluded.
  const sourceParentIds = useMemo(
    () =>
      new Set(
        selectedSubs
          .map((s) => s.parent_id)
          .filter((id): id is number => id !== null),
      ),
    [selectedSubs],
  );

  // Type-compat candidates BEFORE source-parent exclusion. We keep this
  // separate so we can detect the "all compat masters are sources" empty
  // state (no target would be valid even if the user retried).
  const typeCompatMasters = useMemo(() => {
    if (mixedSelection) return [];
    if (soleType === null) return [];
    const sq = filter.trim().toLowerCase();
    return categories
      .filter((c) => c.parent_id === null)
      .filter((c) => c.type === soleType)
      .filter((m) => (sq ? m.name.toLowerCase().includes(sq) : true));
  }, [categories, filter, mixedSelection, soleType]);

  const candidateMasters = useMemo(
    () => typeCompatMasters.filter((m) => !sourceParentIds.has(m.id)),
    [typeCompatMasters, sourceParentIds],
  );

  // True only when the unfiltered (no search) compat list is non-empty but
  // every entry is a source parent. Distinct from "no compat masters at
  // all" so the inline message can be specific.
  const allCompatAreSources = useMemo(() => {
    if (mixedSelection) return false;
    if (soleType === null) return false;
    if (filter.trim().length > 0) return false;
    const allCompat = categories
      .filter((c) => c.parent_id === null)
      .filter((c) => c.type === soleType);
    if (allCompat.length === 0) return false;
    return allCompat.every((m) => sourceParentIds.has(m.id));
  }, [categories, filter, mixedSelection, soleType, sourceParentIds]);

  // Reset internal state when the modal closes or the selection changes.
  useEffect(() => {
    if (!open) {
      setFilter("");
      setTargetId(null);
      setPreview(null);
      setPreviewError("");
      setSubmitError("");
      setRefreshError("");
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

  // Focus trap: initial focus to the filter input, restore on close.
  useFocusTrap({
    active: open,
    containerRef: dialogRef,
    initialFocusRef: filterRef,
  });

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

  async function runRefresh() {
    setRefreshError("");
    try {
      await onSuccess();
    } catch (err) {
      setRefreshError(
        err instanceof Error
          ? `Move succeeded but the page failed to refresh: ${err.message}`
          : "Move succeeded but the page failed to refresh.",
      );
    }
  }

  async function handleConfirm() {
    if (targetId === null) return;
    setSubmitting(true);
    setSubmitError("");
    setRefreshError("");
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
      await runRefresh();
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

        {mixedSelection && (
          <div
            data-testid="batch-move-mixed-warning"
            role="alert"
            className="mt-4 rounded-md bg-warning-dim px-4 py-3 text-sm text-warning"
          >
            Selection mixes income and expense subcategories. Move one type at a time.
          </div>
        )}

        <div className="mt-4">
          <label htmlFor="batch-move-filter" className="sr-only">
            Filter masters
          </label>
          <input
            ref={filterRef}
            id="batch-move-filter"
            type="text"
            placeholder="Filter masters..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className={input}
            disabled={mixedSelection}
          />
        </div>

        <div
          role="radiogroup"
          aria-label="Target master"
          className="mt-3 max-h-56 overflow-y-auto rounded-md border border-border"
        >
          {candidateMasters.length === 0 ? (
            <p
              data-testid="batch-move-empty-message"
              className="p-4 text-xs text-text-muted"
            >
              {mixedSelection
                ? "No targets while the selection mixes types."
                : allCompatAreSources
                  ? "All compatible target masters are already parents of the selected subcategories."
                  : soleType
                    ? `No compatible masters. Selected subcategories require a target of type ${soleType}.`
                    : "No subcategories selected."}
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

        {refreshError && (
          <div
            data-testid="batch-move-refresh-error"
            role="alert"
            className="mt-4 flex items-center justify-between gap-3 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"
          >
            <span>{refreshError}</span>
            <button
              type="button"
              data-testid="batch-move-refresh-retry"
              onClick={() => void runRefresh()}
              className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10"
            >
              Retry
            </button>
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
            disabled={
              targetId === null || submitting || previewing || mixedSelection
            }
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
