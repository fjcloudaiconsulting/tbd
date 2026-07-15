"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
import { btnDangerSolid, btnSecondary, input } from "@/lib/styles";
import type { Category } from "@/lib/types";
import {
  buildFailure,
  compatibleTargets,
  isMigrationTargetRequired,
  type CategoryDeleteResult,
  type FailureRow,
} from "@/components/categories/categoryDeleteHelpers";

interface Props {
  /** The category to delete, or null when the modal is closed. */
  category: Category | null;
  categories: Category[];
  onCancel: () => void;
  /** Awaited after a successful delete so a refresh failure surfaces inline. */
  onSuccess: () => void | Promise<void>;
}

/**
 * Single-category delete with inline reassign.
 *
 * Mirrors the C0 delete contract the way BatchDeleteModal does, but for one
 * category at a time:
 * - If the category has dependents (we can pre-detect transactions via
 *   ``transaction_count``; recurring/forecast dependents are detected lazily
 *   by the backend's 422 ``migration_target_required``), show a migration
 *   target picker and pass ``target_category_id``.
 * - If it has no dependents, delete directly (the backend takes the 204 path).
 * - When a direct delete returns 422 ``migration_target_required`` (recurring
 *   or forecast dependents the client could not see), flip to the picker and
 *   let the user choose a target, then retry.
 *
 * Guard errors (has_children, last_in_type, type_mismatch, name_collision)
 * reuse the same human-readable mapping as the batch flow.
 */
export default function SingleDeleteModal({
  category,
  categories,
  onCancel,
  onSuccess,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [target, setTarget] = useState<number | "">("");
  const [needsTarget, setNeedsTarget] = useState(false);
  const [failure, setFailure] = useState<FailureRow | null>(null);
  const [refreshError, setRefreshError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const open = category !== null;

  // A subcategory with transactions definitely has dependents; show the
  // picker upfront. Masters and zero-transaction subs try the direct path
  // first and fall back to the picker on a 422.
  const hasKnownDependents = (category?.transaction_count ?? 0) > 0;

  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset the reassign target/failure/submit state when the modal opens
      setTarget("");
      setNeedsTarget(hasKnownDependents);
      setFailure(null);
      setRefreshError("");
      setSubmitting(false);
    }
  }, [open, hasKnownDependents]);

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

  useFocusTrap({
    active: open,
    containerRef: dialogRef,
    initialFocusRef: cancelRef,
  });

  const targets = useMemo(
    () => (category ? compatibleTargets(category, categories) : []),
    [category, categories],
  );

  const targetPicked = target !== "" && target !== 0;
  const noTargetAvailable = needsTarget && targets.length === 0;

  async function runRefresh() {
    setRefreshError("");
    try {
      await onSuccess();
    } catch (err) {
      setRefreshError(
        err instanceof Error
          ? `Delete completed but the page failed to refresh: ${err.message}`
          : "Delete completed but the page failed to refresh.",
      );
    }
  }

  async function handleConfirm() {
    if (!category) return;
    if (needsTarget && !targetPicked) {
      setFailure({
        category_id: category.id,
        category_name: category.name,
        reason: "Pick a migration target for this category.",
        reason_code: "migration_target_required",
      });
      return;
    }

    setSubmitting(true);
    setFailure(null);
    setRefreshError("");

    const path =
      needsTarget && targetPicked
        ? `/api/v1/categories/${category.id}?target_category_id=${target}`
        : `/api/v1/categories/${category.id}`;

    try {
      await apiFetch<CategoryDeleteResult | undefined>(path, { method: "DELETE" });
      setSubmitting(false);
      await runRefresh();
    } catch (err) {
      setSubmitting(false);
      // Lazy dependent detection: a direct delete that the backend rejects
      // with migration_target_required means recurring/forecast dependents
      // exist the client could not see. Flip to the picker instead of
      // surfacing a dead-end error.
      if (!needsTarget && isMigrationTargetRequired(err)) {
        setNeedsTarget(true);
        setFailure(null);
        return;
      }
      setFailure(buildFailure(category, err));
    }
  }

  if (!open || !category) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="single-delete-title"
        data-testid="single-delete-modal"
        className="w-full max-w-[min(32rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3
          id="single-delete-title"
          className="text-lg font-semibold text-text-primary"
        >
          Delete {category.name}
        </h3>

        {needsTarget ? (
          <>
            <p
              data-testid="single-delete-reassign-hint"
              className="mt-1 text-sm text-text-secondary"
            >
              This category has transactions, recurring templates, or forecast
              plan items. Pick a category to reassign them to.
            </p>
            {noTargetAvailable ? (
              <p
                data-testid="single-delete-no-target"
                className="mt-4 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"
              >
                No compatible category is available to reassign to. Create a
                same-type master category first, then delete this one.
              </p>
            ) : (
              <div className="mt-4">
                <label
                  htmlFor="single-delete-target"
                  className="mb-1 block text-xs text-text-muted"
                >
                  Reassign to
                </label>
                <select
                  id="single-delete-target"
                  data-testid="single-delete-target"
                  value={target}
                  onChange={(e) =>
                    setTarget(e.target.value === "" ? "" : Number(e.target.value))
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
          </>
        ) : (
          <p className="mt-1 text-sm text-text-secondary">
            Delete this category?
          </p>
        )}

        {failure && (
          <p data-testid="single-delete-failure" className="mt-3 text-xs text-danger">
            {failure.reason}
          </p>
        )}

        {refreshError && (
          <div
            data-testid="single-delete-refresh-error"
            role="alert"
            className="mt-4 flex items-center justify-between gap-3 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"
          >
            <span>{refreshError}</span>
            <button
              type="button"
              data-testid="single-delete-refresh-retry"
              onClick={() => void runRefresh()}
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
            Cancel
          </button>
          <button
            type="button"
            data-testid="single-delete-confirm"
            onClick={handleConfirm}
            disabled={submitting || (needsTarget && !targetPicked)}
            className={`${btnDangerSolid} w-full sm:w-auto min-h-[44px]`}
          >
            {submitting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}
