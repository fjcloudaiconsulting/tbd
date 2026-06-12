"use client";

/**
 * Per-widget filter-chip header row. Renders one pill chip per effective
 * non-default filter the widget queries (computed by
 * ``describeWidgetFilters`` so the chips can never lie about what the
 * widget runs). Each chip is a button that selects the widget and opens
 * the widget popover on the Filters tab via ``onSelectFilters``.
 *
 * Presentational only — it mounts inside ``WidgetShell``. Renders
 * nothing when the widget has no set filters.
 */
import { describeWidgetFilters } from "@/lib/reports/describe-filters";
import type { CanvasFilters, Widget } from "@/lib/reports/types";
import type { Account, Category } from "@/lib/types";

interface Props {
  widget: Widget;
  canvasFilters: CanvasFilters;
  accounts: Account[];
  categories: Category[];
  /** Select the widget + open the popover's Filters tab. */
  onSelectFilters: () => void;
}

// Pill chips share the OverridePill visual register (rounded-full,
// small text). Overridden date chips use the accent register; every
// other chip uses a neutral surface register.
const PILL_BASE =
  "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium transition";
const OVERRIDDEN_CLASS = `${PILL_BASE} bg-accent/15 text-accent hover:bg-accent/25`;
const NEUTRAL_CLASS = `${PILL_BASE} bg-surface-raised text-text-secondary hover:bg-surface`;

export default function WidgetFilterChips({
  widget,
  canvasFilters,
  accounts,
  categories,
  onSelectFilters,
}: Props) {
  const chips = describeWidgetFilters(widget, canvasFilters, {
    accounts,
    categories,
  });

  if (chips.length === 0) return null;

  return (
    <div
      data-testid="widget-filter-chips"
      className="flex flex-wrap gap-1 px-1 pb-1 pt-0.5"
    >
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          data-testid={`widget-filter-chip-${chip.key}`}
          aria-label={`Edit ${chip.key} filter`}
          onClick={(e) => {
            // Stop propagation so the chip's own select-with-Filters
            // action wins over WidgetShell's plain onSelect (which would
            // select the widget WITHOUT requesting the Filters tab).
            e.stopPropagation();
            onSelectFilters();
          }}
          className={chip.overridden ? OVERRIDDEN_CLASS : NEUTRAL_CLASS}
        >
          {chip.label}
        </button>
      ))}
    </div>
  );
}
