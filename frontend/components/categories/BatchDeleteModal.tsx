"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
import { btnDangerSolid, btnSecondary, input } from "@/lib/styles";
import type { Category } from "@/lib/types";
import {
  buildFailure,
  compatibleTargets as compatibleTargetsFor,
  type CategoryDeleteResult,
  type FailureRow,
} from "@/components/categories/categoryDeleteHelpers";

interface Props {
  open: boolean;
  selectedIds: number[];
  categories: Category[];
  onCancel: () => void;
  /** May be async; the modal awaits it and surfaces refresh errors
   *  inline with a Retry control. Mirrors the AddMasterWithSubsModal
   *  pattern so the post-mutation reload never silently drops a
   *  failure. */
  onSuccess: (failures: FailureRow[]) => void | Promise<void>;
}

/**
 * Two-phase batch-delete flow per C0 spec section 4.7 and 7.1:
 * 1. Surface aggregate counts and a per-row migration target picker.
 *    The picker is ALWAYS rendered because the C0 delete contract
 *    requires a migration target whenever the source has dependents
 *    of any kind: transactions, recurring templates, OR forecast plan
 *    items (`category_service.delete_category_with_migration` line ~1188
 *    branches on `_dependent_breakdown.is_empty`). The frontend cannot
 *    cheaply pre-detect non-transaction dependents without a dedicated
 *    preview endpoint, so we always offer the picker; if the source
 *    truly has no dependents the backend takes the 204 path and ignores
 *    the supplied target. Single-delete (handled elsewhere on the page)
 *    is the only path that can omit the target.
 * 2. Loop DELETE /api/v1/categories/{id}?target_category_id={n} per row.
 *    Surface per-row failures with reason (has_children, name_collision,
 *    last_in_type, type_mismatch); allow the user to fix the offending
 *    target and retry only the failures.
 *
 * Type compatibility for the picker mirrors the backend rule at
 * `_check_target_compatibility_for_delete`: INCOME source -> INCOME or
 * BOTH master; EXPENSE source -> EXPENSE or BOTH master. BOTH source
 * defaults to BOTH master in the picker (the lenient breakdown-driven
 * path can only be reasoned about by the backend, so we present the
 * safe default and let the user retry if the backend rejects it).
 *
 * Owned by Team Categories C2 UI.
 */
export default function BatchDeleteModal({
  open,
  selectedIds,
  categories,
  onCancel,
  onSuccess,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [migrationTargets, setMigrationTargets] = useState<Record<number, number | "">>({});
  const [pendingIds, setPendingIds] = useState<number[]>([]);
  const [failures, setFailures] = useState<FailureRow[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [globalError, setGlobalError] = useState("");
  const [refreshError, setRefreshError] = useState<string>("");
  const [lastFailures, setLastFailures] = useState<FailureRow[] | null>(null);

  const selectedSubs = useMemo(
    () => categories.filter((c) => selectedIds.includes(c.id) && c.parent_id !== null),
    [categories, selectedIds],
  );

  // Reset when opening / selection changes.
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset migration targets/pending/failure state when the modal opens or the selection changes
      setMigrationTargets({});
      setPendingIds(selectedSubs.map((s) => s.id));
      setFailures([]);
      setGlobalError("");
      setRefreshError("");
      setSubmitting(false);
      setLastFailures(null);
    }
  }, [open, selectedSubs]);

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

  // Focus trap: initial focus to Cancel (Close) so a stray Enter does
  // not arm a destructive action.
  useFocusTrap({
    active: open,
    containerRef: dialogRef,
    initialFocusRef: cancelRef,
  });

  const rowsToShow = useMemo(
    () => selectedSubs.filter((s) => pendingIds.includes(s.id)),
    [selectedSubs, pendingIds],
  );

  const aggregate = useMemo(() => {
    let withTx = 0;
    let txCount = 0;
    for (const s of rowsToShow) {
      if (s.transaction_count > 0) withTx += 1;
      txCount += s.transaction_count;
    }
    return { withTx, txCount };
  }, [rowsToShow]);

  // Per-row compatible-target list. INCOME source -> INCOME or BOTH
  // target. EXPENSE source -> EXPENSE or BOTH target. BOTH source falls
  // back to BOTH target (the safe default for the breakdown-driven
  // backend rule). The candidate list also excludes the source itself
  // and excludes other selected subs (so the user cannot pick a sub
  // about to be deleted as the migration target).
  function compatibleTargets(sub: Category): Category[] {
    const otherSelected = new Set(rowsToShow.map((s) => s.id));
    return compatibleTargetsFor(sub, categories, otherSelected);
  }

  // Submit is gated on every row having a migration target picked.
  // The backend takes the 204 path and ignores the target when the
  // source has no dependents, so the extra arg is harmless when not
  // needed.
  const allTargetsPicked = useMemo(
    () =>
      rowsToShow.every((s) => {
        const v = migrationTargets[s.id];
        return v !== undefined && v !== "" && v !== 0;
      }),
    [rowsToShow, migrationTargets],
  );

  async function runRefresh(failuresToReport: FailureRow[]) {
    setRefreshError("");
    try {
      await onSuccess(failuresToReport);
    } catch (err) {
      setRefreshError(
        err instanceof Error
          ? `Delete completed but the page failed to refresh: ${err.message}`
          : "Delete completed but the page failed to refresh.",
      );
    }
  }

  async function handleConfirm() {
    setSubmitting(true);
    setGlobalError("");
    setRefreshError("");
    const newFailures: FailureRow[] = [];
    const succeeded: number[] = [];

    for (const sub of rowsToShow) {
      const targetId = migrationTargets[sub.id];

      if (targetId === undefined || targetId === "" || targetId === 0) {
        newFailures.push({
          category_id: sub.id,
          category_name: sub.name,
          reason: "Pick a migration target for this subcategory.",
          reason_code: "migration_target_required",
        });
        continue;
      }

      const path = `/api/v1/categories/${sub.id}?target_category_id=${targetId}`;

      try {
        await apiFetch<CategoryDeleteResult | undefined>(path, { method: "DELETE" });
        succeeded.push(sub.id);
      } catch (err) {
        newFailures.push(buildFailure(sub, err));
      }
    }

    setSubmitting(false);

    // Drop succeeded rows from the pending set; keep failures listed for retry.
    if (newFailures.length === 0) {
      setFailures([]);
      setPendingIds([]);
      setLastFailures([]);
      await runRefresh([]);
      return;
    }

    setFailures(newFailures);
    setPendingIds(newFailures.map((f) => f.category_id));
    setLastFailures(newFailures);

    if (succeeded.length > 0) {
      // Notify parent so it can refresh and clear succeeded ids from selection.
      await runRefresh(newFailures);
    }
  }

  if (!open) return null;

  const allDone = pendingIds.length === 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="batch-delete-title"
        data-testid="batch-delete-modal"
        className="w-full max-w-[min(36rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="batch-delete-title" className="text-lg font-semibold text-text-primary">
          Delete {rowsToShow.length} subcategor{rowsToShow.length === 1 ? "y" : "ies"}
        </h3>
        <p
          data-testid="batch-delete-aggregate"
          className="mt-1 text-sm text-text-secondary"
        >
          {aggregate.withTx === 0 ? (
            <>
              Pick a migration target for each subcategory. Recurring templates and forecast plan items also reassign through the target.
            </>
          ) : (
            <>
              {aggregate.withTx} of {rowsToShow.length} hold{" "}
              {aggregate.withTx === 1 ? "a referencing transaction" : "referencing transactions"}{" "}
              ({aggregate.txCount} total). Pick a migration target for each.
            </>
          )}
        </p>

        <div className="mt-4 space-y-3">
          {rowsToShow.map((sub) => {
            const failure = failures.find((f) => f.category_id === sub.id);
            const targets = compatibleTargets(sub);
            const value = migrationTargets[sub.id] ?? "";
            return (
              <div
                key={sub.id}
                data-testid={`batch-delete-row-${sub.id}`}
                className={`rounded-md border p-3 ${
                  failure ? "border-danger" : "border-border"
                }`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-text-primary">
                      {sub.name}
                    </p>
                    <p className="text-xs text-text-muted">
                      {sub.transaction_count} transaction
                      {sub.transaction_count === 1 ? "" : "s"} . type {sub.type}
                    </p>
                  </div>
                </div>

                <div className="mt-2">
                  <label
                    htmlFor={`batch-delete-target-${sub.id}`}
                    className="mb-1 block text-xs text-text-muted"
                  >
                    Migrate to
                  </label>
                  <select
                    id={`batch-delete-target-${sub.id}`}
                    data-testid={`batch-delete-target-${sub.id}`}
                    value={value}
                    onChange={(e) =>
                      setMigrationTargets((prev) => ({
                        ...prev,
                        [sub.id]: e.target.value === "" ? "" : Number(e.target.value),
                      }))
                    }
                    className={input}
                  >
                    <option value="">Select a master...</option>
                    {targets.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name} ({t.type})
                      </option>
                    ))}
                  </select>
                </div>

                {failure && (
                  <p
                    data-testid={`batch-delete-failure-${sub.id}`}
                    className="mt-2 text-xs text-danger"
                  >
                    {failure.reason}
                  </p>
                )}
              </div>
            );
          })}
        </div>

        {globalError && (
          <div className="mt-4 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
            {globalError}
          </div>
        )}

        {refreshError && (
          <div
            data-testid="batch-delete-refresh-error"
            role="alert"
            className="mt-4 flex items-center justify-between gap-3 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"
          >
            <span>{refreshError}</span>
            <button
              type="button"
              data-testid="batch-delete-refresh-retry"
              onClick={() => void runRefresh(lastFailures ?? failures)}
              className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10"
            >
              Retry
            </button>
          </div>
        )}

        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px]`}
          >
            Close
          </button>
          {!allDone && (
            <button
              type="button"
              data-testid="batch-delete-confirm"
              onClick={handleConfirm}
              disabled={submitting || !allTargetsPicked}
              className={`${btnDangerSolid} w-full sm:w-auto min-h-[44px]`}
            >
              {submitting
                ? "Deleting..."
                : failures.length > 0
                  ? `Retry ${rowsToShow.length}`
                  : `Delete ${rowsToShow.length}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

