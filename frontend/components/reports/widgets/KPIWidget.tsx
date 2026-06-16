"use client";

/**
 * KPI widget — single big number with an optional delta vs the
 * prior period. Pulls its rows through ``useReportQuery``; failure
 * renders an inline error inside this card and does not bubble to
 * sibling widgets.
 *
 * Visual register intentionally minimal — a label, a value, an
 * optional delta. Live in the canvas grid; the widget shell (drag
 * handle, title bar) wraps it.
 */
import { useReportQuery } from "@/lib/reports/useReportQuery";
import { formatMeasureValue } from "@/lib/reports/series";
import type { CanvasFilters, KPIWidget as KPIWidgetType } from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import type { CsvCell } from "@/lib/reports/csv";

interface Props {
  widget: KPIWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /**
   * Optional injected prior-period value. Computed by the editor
   * page (which can resolve the prior-period date range from the
   * effective filters); the widget itself doesn't recompute the
   * date arithmetic. Undefined means "no delta shown."
   */
  priorValue?: number | null;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function KPIWidget({
  widget,
  canvasFilters,
  editMode,
  priorValue,
  currency,
}: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const value = readValue(data?.rows[0]);
  const format = widget.config.format ?? "number";
  const showDelta =
    widget.config.compare_prior_period === true &&
    priorValue !== undefined &&
    priorValue !== null &&
    value !== null;
  const delta =
    showDelta && value !== null && priorValue !== null && priorValue !== 0
      ? ((value - priorValue) / Math.abs(priorValue)) * 100
      : null;

  // CSV export: a single label/value row (the KPI is one number).
  const measureLabel = widget.config.measure.field;
  const csvDataset = {
    headers: [widget.title || "KPI", measureLabel],
    rows:
      value === null
        ? ([] as CsvCell[][])
        : ([[widget.title || "KPI", value]] as CsvCell[][]),
  };

  return (
    <div
      data-testid="kpi-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col justify-center gap-1 rounded-lg border border-border bg-surface p-4"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
          {widget.title || "KPI"}
        </div>
        <WidgetCsvButton
          title={widget.title || "KPI"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      {isLoading ? (
        <div
          data-testid="kpi-widget-loading"
          className="h-7 w-24 animate-pulse rounded bg-border"
        />
      ) : error ? (
        <div
          role="alert"
          data-testid="kpi-widget-error"
          className="text-sm text-danger"
        >
          Couldn&apos;t load
        </div>
      ) : value === null ? (
        <div className="text-2xl font-semibold text-text-muted">—</div>
      ) : (
        <>
          <div
            data-testid="kpi-widget-value"
            className="text-2xl font-semibold text-text-primary"
          >
            {formatMeasureValue(value, format, currency)}
          </div>
          {showDelta && delta !== null && (
            <div
              data-testid="kpi-widget-delta"
              className={
                delta >= 0
                  ? "text-xs font-medium text-success"
                  : "text-xs font-medium text-danger"
              }
            >
              {delta >= 0 ? "+" : ""}
              {delta.toFixed(1)}% vs prior period
            </div>
          )}
        </>
      )}
    </div>
  );
}

function readValue(
  row: Record<string, string | number | null> | undefined,
): number | null {
  if (!row) return null;
  const v = row.value;
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}
