"use client";

import { useEffect, useRef } from "react";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
import { btnPrimary, btnSecondary } from "@/lib/styles";

/**
 * Two-step drag-move confirmation modal for C2b. The drag-end handler
 * on /categories opens this modal once a valid drop has been
 * classified; the modal renders the preview impact (or its loading /
 * error state), the surfaced backend error if the PATCH fails, and a
 * primary Confirm action.
 *
 * Modal a11y matches the rest of the codebase's modal contract (see
 * `BatchMoveModal` + `ConfirmModal` for the original implementations):
 *   - focus moves into the dialog on open (initial focus goes to the
 *     Cancel button so a stray Enter from the keyboard drag does NOT
 *     immediately submit a move the user might not have intended);
 *   - Tab is trapped inside the dialog;
 *   - Escape closes the modal (and routes through onCancel);
 *   - focus restores on close;
 *   - body scroll locks while open.
 *
 * Owned by Team Categories C2 UI (C2b).
 */

export interface MovePreviewSummary {
  affected_transaction_count: number;
  affected_recurring_count: number;
  affected_forecast_item_count: number;
  budget_actuals_shifted: boolean;
}

interface Props {
  open: boolean;
  subcategoryName: string;
  targetMasterName: string;
  preview: MovePreviewSummary | null;
  previewLoading: boolean;
  previewError: string;
  moveError: string;
  submitting: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function DragMoveConfirmModal({
  open,
  subcategoryName,
  targetMasterName,
  preview,
  previewLoading,
  previewError,
  moveError,
  submitting,
  onConfirm,
  onCancel,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useFocusTrap({
    active: open,
    containerRef: dialogRef,
    initialFocusRef: cancelRef,
  });

  // Escape closes the modal. Stops propagation so the page-level Esc
  // handler (which would also exit Edit mode) does not fire.
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

  // Body scroll lock while open.
  useEffect(() => {
    if (!open) return;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      data-testid="drag-move-confirm"
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drag-move-confirm-title"
        className="w-full max-w-[min(28rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3
          id="drag-move-confirm-title"
          className="text-lg font-semibold text-text-primary"
        >
          Move subcategory
        </h3>
        <p className="mt-2 text-sm text-text-secondary">
          Move <strong>{subcategoryName}</strong> to{" "}
          <strong>{targetMasterName}</strong>?
        </p>

        <div
          data-testid="drag-move-preview"
          className="mt-4 rounded-md border border-border bg-surface-raised p-3 text-sm text-text-secondary"
        >
          {previewLoading ? (
            <span>Loading preview...</span>
          ) : previewError ? (
            <span className="text-danger">{previewError}</span>
          ) : preview ? (
            <>
              <p>
                Reassigns <strong>{preview.affected_transaction_count}</strong>{" "}
                transaction
                {preview.affected_transaction_count === 1 ? "" : "s"},{" "}
                <strong>{preview.affected_recurring_count}</strong> recurring
                template
                {preview.affected_recurring_count === 1 ? "" : "s"}, and{" "}
                <strong>{preview.affected_forecast_item_count}</strong>{" "}
                forecast plan item
                {preview.affected_forecast_item_count === 1 ? "" : "s"}.
              </p>
              {preview.budget_actuals_shifted && (
                <p className="mt-1 text-xs text-text-muted">
                  Current-period budget actuals will shift attribution. Planned
                  amounts are unchanged.
                </p>
              )}
            </>
          ) : null}
        </div>

        {moveError && (
          <div
            data-testid="drag-move-error"
            role="alert"
            className="mt-4 whitespace-pre-line rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"
          >
            {moveError}
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
            data-testid="drag-move-confirm-button"
            onClick={onConfirm}
            disabled={submitting || previewLoading || preview === null}
            className={`${btnPrimary} w-full sm:w-auto`}
          >
            {submitting ? "Moving..." : "Move"}
          </button>
        </div>
      </div>
    </div>
  );
}
