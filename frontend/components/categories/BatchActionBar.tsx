"use client";

import { btnDangerSolid, btnSecondary } from "@/lib/styles";

interface Props {
  count: number;
  onMove: () => void;
  onDelete: () => void;
  onClear: () => void;
}

/**
 * Sticky bottom-of-page batch-action bar. Visible only when at least one
 * subcategory is selected in Edit mode. Owned by Team Categories C2 UI.
 */
export default function BatchActionBar({ count, onMove, onDelete, onClear }: Props) {
  if (count < 1) return null;

  return (
    <div
      data-testid="batch-action-bar"
      role="toolbar"
      aria-label="Batch actions"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface-raised"
    >
      <div className="mx-auto flex max-w-6xl flex-col items-stretch gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-3 md:px-6">
        <div className="flex items-center gap-3">
          <span
            data-testid="batch-action-count"
            className="text-sm font-medium text-text-primary"
          >
            {count} selected
          </span>
          <button
            type="button"
            onClick={onClear}
            className="text-xs text-text-muted hover:text-accent"
          >
            Clear
          </button>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-2">
          <button
            type="button"
            data-testid="batch-move-button"
            onClick={onMove}
            className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
          >
            Move {count} to...
          </button>
          <button
            type="button"
            data-testid="batch-delete-button"
            onClick={onDelete}
            className={`${btnDangerSolid} min-h-[44px] sm:min-h-0`}
          >
            Delete {count}
          </button>
        </div>
      </div>
    </div>
  );
}
