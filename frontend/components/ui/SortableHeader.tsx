"use client";

// SortableHeader: a presentational sortable column header cell.
//
// Renders a <th> with a button inside. The button shows the column label and,
// when this is the active sort column, a directional indicator (▲/▼).
// Clicking calls onSort(field) — the parent decides toggle logic (e.g. flip
// direction or switch column) so this component stays stateless and reusable.
//
// aria-sort is placed on the <th> (columnheader) element as per ARIA spec.

import type { SortDir } from "@/lib/hooks/use-persisted-sort";

export interface SortableHeaderProps {
  label: string;
  field: string;
  activeField: string;
  dir: SortDir;
  onSort: (field: string) => void;
  align?: "left" | "right";
}

export default function SortableHeader({
  label,
  field,
  activeField,
  dir,
  onSort,
  align = "left",
}: SortableHeaderProps) {
  const isActive = field === activeField;

  const ariaSort = isActive
    ? dir === "asc"
      ? "ascending"
      : "descending"
    : "none";

  const indicator = isActive ? (dir === "asc" ? "▲" : "▼") : null;

  return (
    <th
      aria-sort={ariaSort}
      className={`px-3 py-2 text-xs font-medium text-text-secondary ${align === "right" ? "text-right" : "text-left"}`}
    >
      <button
        type="button"
        onClick={() => onSort(field)}
        className="inline-flex items-center gap-1 hover:text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 rounded"
      >
        {label}
        {indicator !== null && (
          <span aria-hidden="true" className="text-[10px]">
            {indicator}
          </span>
        )}
      </button>
    </th>
  );
}
