"use client";

/**
 * Multi-series measure editor (one row per ``config.measures`` entry, with
 * add/remove and a per-type cap) for line / area / stacked_bar / table.
 * Extracted verbatim from the original widget config rail.
 */
import HelpTooltip from "@/components/help/HelpTooltip";
import {
  AGG_HELP_KEY,
  AGG_OPTIONS,
  FIELD_OPTIONS,
  MAX_SERIES,
  MAX_TABLE_COLUMNS,
} from "@/components/reports/config/controlConstants";
import type {
  Aggregation,
  AreaConfig,
  LineConfig,
  MeasureField,
  SeriesConfig,
  StackedBarConfig,
  TableConfig,
  Widget,
} from "@/lib/reports/types";

export default function MeasuresEditor({
  widget,
  onChange,
}: {
  widget: Widget & { config: LineConfig | AreaConfig | StackedBarConfig | TableConfig };
  onChange: (m: SeriesConfig[]) => void;
}) {
  const measures = widget.config.measures;
  const cap = widget.type === "table" ? MAX_TABLE_COLUMNS : MAX_SERIES;

  function update(idx: number, next: SeriesConfig) {
    const copy = [...measures];
    copy[idx] = next;
    onChange(copy);
  }

  function add() {
    if (measures.length >= cap) return;
    onChange([
      ...measures,
      { measure: { agg: "sum", field: "amount" } },
    ]);
  }

  function remove(idx: number) {
    if (measures.length <= 1) return;
    onChange(measures.filter((_, i) => i !== idx));
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        {widget.type === "table" ? "Columns" : "Series"}
      </div>
      {measures.map((s, idx) => (
        <div
          key={idx}
          data-testid={`measure-row-${idx}`}
          className="flex flex-col gap-1 rounded-md border border-border bg-bg p-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-muted">
              {widget.type === "table" ? `Column ${idx + 1}` : `Series ${idx + 1}`}
            </span>
            {measures.length > 1 && (
              <button
                type="button"
                data-testid={`measure-remove-${idx}`}
                onClick={() => remove(idx)}
                className="text-xs text-text-muted hover:text-danger"
                aria-label={`Remove ${widget.type === "table" ? "column" : "series"} ${idx + 1}`}
              >
                Remove
              </button>
            )}
          </div>
          <input
            type="text"
            value={s.label ?? ""}
            onChange={(e) =>
              update(idx, { ...s, label: e.target.value || undefined })
            }
            placeholder={
              widget.type === "table" ? "Column label" : "Series label"
            }
            aria-label={`Series ${idx + 1} label`}
            className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
          />
          <div className="flex items-center gap-1">
            <select
              value={s.measure.agg}
              onChange={(e) =>
                update(idx, {
                  ...s,
                  measure: { ...s.measure, agg: e.target.value as Aggregation },
                })
              }
              aria-label={`Series ${idx + 1} aggregation`}
              className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
            >
              {AGG_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <HelpTooltip k={AGG_HELP_KEY[s.measure.agg]} />
            <select
              value={s.measure.field}
              onChange={(e) =>
                update(idx, {
                  ...s,
                  measure: {
                    ...s.measure,
                    field: e.target.value as MeasureField,
                  },
                })
              }
              aria-label={`Series ${idx + 1} field`}
              className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
            >
              {FIELD_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      ))}
      {measures.length < cap && (
        <button
          type="button"
          data-testid="measure-add"
          onClick={add}
          className="rounded-md border border-dashed border-border px-2 py-1 text-xs text-text-secondary transition hover:border-accent hover:text-accent"
        >
          + Add {widget.type === "table" ? "column" : "series"}
        </button>
      )}
    </div>
  );
}
