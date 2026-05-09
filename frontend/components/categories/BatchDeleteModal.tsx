"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, ApiResponseError, extractErrorMessage } from "@/lib/api";
import { btnSecondary, input } from "@/lib/styles";
import type { Category } from "@/lib/types";

interface CategoryDeleteResult {
  deleted_category_id: number;
  migration_target_id: number | null;
  migrated_transaction_count: number;
  migrated_recurring_count: number;
  migrated_forecast_item_count: number;
  deleted_rule_count: number;
}

interface FailureRow {
  category_id: number;
  category_name: string;
  reason: string;
  reason_code?: string;
}

interface Props {
  open: boolean;
  selectedIds: number[];
  categories: Category[];
  onCancel: () => void;
  onSuccess: (failures: FailureRow[]) => void;
}

/**
 * Two-phase batch-delete flow per C0 spec section 4.7 and 7.1:
 * 1. Surface aggregate counts. For categories that report a non-zero
 *    transaction_count we expose a per-category migration target picker.
 * 2. Loop DELETE /api/v1/categories/{id}?target_category_id={n} per row.
 *    Surface per-row failures with reason (has_children, name_collision,
 *    last_in_type, type_mismatch); allow the user to fix the offending
 *    target and retry only the failures.
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
  const [migrationTargets, setMigrationTargets] = useState<Record<number, number | "">>({});
  const [pendingIds, setPendingIds] = useState<number[]>([]);
  const [failures, setFailures] = useState<FailureRow[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [globalError, setGlobalError] = useState("");

  const selectedSubs = useMemo(
    () => categories.filter((c) => selectedIds.includes(c.id) && c.parent_id !== null),
    [categories, selectedIds],
  );

  // Reset when opening / selection changes.
  useEffect(() => {
    if (open) {
      setMigrationTargets({});
      setPendingIds(selectedSubs.map((s) => s.id));
      setFailures([]);
      setGlobalError("");
      setSubmitting(false);
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

  const rowsToShow = useMemo(
    () => selectedSubs.filter((s) => pendingIds.includes(s.id)),
    [selectedSubs, pendingIds],
  );

  const aggregate = useMemo(() => {
    let withDeps = 0;
    let txCount = 0;
    for (const s of rowsToShow) {
      if (s.transaction_count > 0) withDeps += 1;
      txCount += s.transaction_count;
    }
    return { withDeps, txCount };
  }, [rowsToShow]);

  // Build a per-row compatible-target list. INCOME requires INCOME or BOTH.
  // EXPENSE requires EXPENSE or BOTH. BOTH defaults to BOTH for safety
  // (matches spec section 4.6 frontend behaviour).
  function compatibleTargets(sub: Category): Category[] {
    return categories.filter((c) => {
      if (c.id === sub.id) return false;
      if (c.parent_id !== null) return false;
      if (sub.type === "income") return c.type === "income" || c.type === "both";
      if (sub.type === "expense") return c.type === "expense" || c.type === "both";
      return c.type === "both";
    });
  }

  async function handleConfirm() {
    setSubmitting(true);
    setGlobalError("");
    const newFailures: FailureRow[] = [];
    const succeeded: number[] = [];

    for (const sub of rowsToShow) {
      const needsTarget = sub.transaction_count > 0;
      const targetId = migrationTargets[sub.id];

      if (needsTarget && (targetId === undefined || targetId === "" || targetId === 0)) {
        newFailures.push({
          category_id: sub.id,
          category_name: sub.name,
          reason: "Pick a migration target for this subcategory.",
          reason_code: "migration_target_required",
        });
        continue;
      }

      const path =
        needsTarget && targetId
          ? `/api/v1/categories/${sub.id}?target_category_id=${targetId}`
          : `/api/v1/categories/${sub.id}`;

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
      onSuccess([]);
      return;
    }

    setFailures(newFailures);
    setPendingIds(newFailures.map((f) => f.category_id));

    if (succeeded.length > 0) {
      // Notify parent so it can refresh and clear succeeded ids from selection.
      onSuccess(newFailures);
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
          {aggregate.withDeps === 0 ? (
            <>No referenced transactions. Subcategories will be removed.</>
          ) : (
            <>
              {aggregate.withDeps} of {rowsToShow.length} hold{" "}
              {aggregate.withDeps === 1 ? "a referencing transaction" : "referencing transactions"}{" "}
              ({aggregate.txCount} total). Pick a migration target for each.
            </>
          )}
        </p>

        <div className="mt-4 space-y-3">
          {rowsToShow.map((sub) => {
            const failure = failures.find((f) => f.category_id === sub.id);
            const needsTarget = sub.transaction_count > 0;
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

                {needsTarget && (
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
                )}

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

        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
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
              disabled={submitting}
              className="rounded-md bg-danger px-4 py-2 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50 w-full sm:w-auto min-h-[44px]"
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

interface CategoryErrorDetail {
  detail?: string;
  conflicting_child_name?: string;
  scope?: string;
  type?: string;
  child_names?: string[];
  source_type?: string;
  target_type?: string;
  dependent_breakdown?: { income: number; expense: number };
}

function buildFailure(sub: Category, err: unknown): FailureRow {
  if (err instanceof ApiResponseError) {
    const detail = err.detail as CategoryErrorDetail | string | undefined;
    if (typeof detail === "object" && detail !== null) {
      if (detail.detail === "last_in_type") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Cannot delete the only ${detail.type ?? ""} ${detail.scope ?? "subcategory"}.`,
          reason_code: "last_in_type",
        };
      }
      if (detail.detail === "has_children") {
        const sample = detail.child_names?.[0] ?? "subcategory";
        const more = (detail.child_names?.length ?? 0) - 1;
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Has subcategories. Move or delete "${sample}"${
            more > 0 ? ` and ${more} other${more === 1 ? "" : "s"}` : ""
          } first.`,
          reason_code: "has_children",
        };
      }
      if (detail.detail === "type_mismatch") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Migration target type ${detail.target_type ?? ""} is not compatible with ${detail.source_type ?? "source"}.`,
          reason_code: "type_mismatch",
        };
      }
      if (detail.detail === "name_collision") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Name collision: "${detail.conflicting_child_name ?? sub.name}" already exists on the target.`,
          reason_code: "name_collision",
        };
      }
      if (detail.detail === "migration_target_required") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Migration target required.`,
          reason_code: "migration_target_required",
        };
      }
    }
    return {
      category_id: sub.id,
      category_name: sub.name,
      reason: err.message || "Delete failed",
    };
  }
  return {
    category_id: sub.id,
    category_name: sub.name,
    reason: extractErrorMessage(err, "Delete failed"),
  };
}
