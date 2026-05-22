"use client";

/**
 * Canvas-wide filters row — sits above the canvas. Edits flow into
 * the report's ``canvas_filters_json`` blob and cascade to every
 * widget that doesn't override the same field (spec §4).
 *
 * v1 controls: date range (start + end), account ids (comma-list),
 * category ids (comma-list). PR3 swaps the comma-list inputs for
 * the proper chip pickers + tree picker.
 */
import type { CanvasFilters } from "@/lib/reports/types";

interface Props {
  value: CanvasFilters;
  onChange: (next: CanvasFilters) => void;
}

export default function CanvasFiltersBar({ value, onChange }: Props) {
  return (
    <div
      data-testid="canvas-filters-bar"
      className="flex flex-wrap items-end gap-3 rounded-md border border-border bg-surface px-4 py-3"
    >
      <Field label="Date from">
        <input
          type="date"
          aria-label="Canvas date from"
          value={value.date_range?.start ?? ""}
          onChange={(e) =>
            onChange({
              ...value,
              date_range: {
                ...(value.date_range ?? {}),
                start: e.target.value || undefined,
              },
            })
          }
          className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </Field>
      <Field label="Date to">
        <input
          type="date"
          aria-label="Canvas date to"
          value={value.date_range?.end ?? ""}
          onChange={(e) =>
            onChange({
              ...value,
              date_range: {
                ...(value.date_range ?? {}),
                end: e.target.value || undefined,
              },
            })
          }
          className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </Field>
      <Field label="Accounts (ids)">
        <input
          type="text"
          inputMode="numeric"
          aria-label="Canvas accounts"
          placeholder="e.g. 12, 14"
          value={(value.account_ids ?? []).join(",")}
          onChange={(e) =>
            onChange({
              ...value,
              account_ids: parseIdList(e.target.value),
            })
          }
          className="w-32 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </Field>
      <Field label="Categories (ids)">
        <input
          type="text"
          inputMode="numeric"
          aria-label="Canvas categories"
          placeholder="e.g. 3, 5"
          value={(value.category_ids ?? []).join(",")}
          onChange={(e) =>
            onChange({
              ...value,
              category_ids: parseIdList(e.target.value),
            })
          }
          className="w-32 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </Field>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        {label}
      </span>
      {children}
    </label>
  );
}

function parseIdList(raw: string): number[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => Number(s))
    .filter((n) => Number.isInteger(n) && n > 0);
}
