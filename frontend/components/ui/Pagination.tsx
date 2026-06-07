"use client";

// Pagination: presentational pagination controls for row tables.
//
// Renders a per-page size selector, Previous/Next navigation buttons, and a
// status line ("Page X of Y · N total"). The component is deliberately
// stateless — all state lives in the parent (typically via useTableState).
//
// Copy rule: no em-dashes anywhere in user-visible text. Use the middot (·)
// as the status separator, commas, periods, or parentheses everywhere else.

import { useId } from "react";

import { pageCount, PAGE_SIZE_OPTIONS } from "@/lib/hooks/use-table-state";

export interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (n: number) => void;
  onPageSizeChange: (n: number) => void;
  pageSizeOptions?: number[];
  // When false, the per-page selector is omitted. An empty <div /> is still
  // rendered as the first flex child so justify-between keeps the status +
  // navigation group right-aligned (dropping the child would left-align them).
  showPageSizeSelector?: boolean;
}

export default function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [...PAGE_SIZE_OPTIONS],
  showPageSizeSelector = true,
}: PaginationProps) {
  const uid = useId();
  const selectId = `pagination-page-size-${uid}`;
  const totalPages = pageCount(total, pageSize);
  const isFirst = page <= 1;
  const isLast = page >= totalPages;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm text-text-secondary">
      {/* Per-page selector. When hidden, render an empty <div /> so the
          justify-between layout keeps the status + nav group right-aligned. */}
      {showPageSizeSelector ? (
        <div className="flex items-center gap-2">
          <label
            htmlFor={selectId}
            className="whitespace-nowrap text-xs"
          >
            Per page
          </label>
          <select
            id={selectId}
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            className="rounded border border-border bg-surface px-2 py-1 text-xs text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
          >
            {pageSizeOptions.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div />
      )}

      {/* Status + navigation */}
      <div className="flex items-center gap-3">
        {/* Status line — middot (·) separator, no em-dashes */}
        <span className="whitespace-nowrap text-xs">
          Page {page} of {totalPages} &middot; {total} total
        </span>

        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Previous page"
            disabled={isFirst}
            onClick={() => onPageChange(page - 1)}
            className="inline-flex items-center justify-center rounded border border-border px-2.5 py-1 text-xs hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 min-h-[32px]"
          >
            Previous
          </button>
          <button
            type="button"
            aria-label="Next page"
            disabled={isLast}
            onClick={() => onPageChange(page + 1)}
            className="inline-flex items-center justify-center rounded border border-border px-2.5 py-1 text-xs hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 min-h-[32px]"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
