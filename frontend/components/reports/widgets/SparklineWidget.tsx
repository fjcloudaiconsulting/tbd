"use client";

/**
 * Sparkline widget — compact trend line designed for a small grid
 * slot. No axes, no legend, no axis rulers. Tooltip on hover. The
 * widget surfaces the LAST data point as a big number alongside the
 * trend line so a single glance gives both the latest value and the
 * direction of travel.
 *
 * Single dimension, single aggregation — the config rail locks both
 * when the widget type is ``sparkline``.
 *
 * The recharts subtree is code-split via ``next/dynamic`` (ssr:false)
 * into ``SparklineWidgetChart`` so recharts loads only when a sparkline
 * mounts, keeping it out of the route's initial JS.
 */
import dynamic from "next/dynamic";

import { useReportQuery } from "@/lib/reports/useReportQuery";
import { dimensionHeader, formatMeasureValue } from "@/lib/reports/series";
import type {
  CanvasFilters,
  SparklineWidget as SparklineWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import type { CsvCell } from "@/lib/reports/csv";

const SparklineWidgetChart = dynamic(
  () => import("./SparklineWidgetChart"),
  {
    ssr: false,
    loading: () => (
      <div
        data-testid="sparkline-widget-chart-loading"
        className="h-full w-full animate-pulse rounded bg-border/40"
      />
    ),
  },
);

interface Props {
  widget: SparklineWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function SparklineWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const rows = (data?.rows ?? []).map((r) => ({
    label: String(r[dimensionKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));
  const lastValue = rows.length > 0 ? rows[rows.length - 1].value : null;
  const format = widget.config.format ?? "number";

  // CSV export mirrors the underlying trend series: [dimension, measure].
  const measureLabel = widget.config.measure.field;
  const csvDataset = {
    headers: [dimensionHeader(dimensionKey), measureLabel],
    rows: rows.map((r) => [r.label, r.value]) as CsvCell[][],
  };

  return (
    <div
      data-testid="sparkline-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col justify-center gap-1 rounded-lg border border-border bg-surface p-3"
      aria-label={widget.title || "Sparkline"}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
          {widget.title || "Sparkline"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Sparkline"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      {isLoading ? (
        <div
          data-testid="sparkline-widget-loading"
          className="h-6 w-20 animate-pulse rounded bg-border"
        />
      ) : error ? (
        <div
          role="alert"
          data-testid="sparkline-widget-error"
          className="text-sm text-danger"
        >
          Couldn&apos;t load
        </div>
      ) : rows.length === 0 ? (
        <div
          data-testid="sparkline-widget-empty"
          className="text-2xl font-semibold text-text-muted"
        >—</div>
      ) : (
        <>
          <div
            data-testid="sparkline-widget-value"
            className="text-xl font-semibold text-text-primary"
          >
            {formatMeasureValue(lastValue ?? 0, format, currency)}
          </div>
          <div className="-mx-1 h-10">
            <SparklineWidgetChart rows={rows} format={format} currency={currency} />
          </div>
        </>
      )}
    </div>
  );
}
