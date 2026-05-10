"use client";

import { RotateCcw } from "lucide-react";

/**
 * Reset affordance for sort + filter persistence.
 *
 * Visible only when sort or any filter differs from defaults (the parent
 * passes `visible`). Clicking calls the supplied handler, which is expected
 * to call both `sort.reset()` and `filters.reset()` from the persistence
 * hooks so the localStorage entries clear at the same time the in-memory
 * state reverts.
 *
 * Intentionally small and dependency-free; styling matches the sibling
 * filter row pills so the button reads as part of the toolbar rather than a
 * destructive action.
 */
export default function ResetSortFiltersButton({
  visible,
  onClick,
  label = "Reset filters and sort",
  className = "",
}: {
  visible: boolean;
  onClick: () => void;
  label?: string;
  className?: string;
}) {
  if (!visible) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised min-h-[44px] sm:min-h-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${className}`}
      aria-label={label}
      data-testid="reset-sort-filters"
    >
      <RotateCcw className="h-3 w-3" aria-hidden="true" />
      {label}
    </button>
  );
}
