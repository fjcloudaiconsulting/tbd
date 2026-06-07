"use client";

/**
 * Pie widget — share-of-total over a single dimension. The spec caps
 * the visible slice count: anything beyond ``top_n`` (default 8) is
 * rolled into a single "Other" slice. Legend renders below the pie.
 *
 * Single dimension, single aggregation — the config rail locks both
 * to length 1 when the widget type is ``pie``.
 *
 * The recharts subtree is code-split via ``next/dynamic`` (ssr:false)
 * into ``PieWidgetChart`` so recharts loads only when a chart mounts,
 * keeping it out of the route's initial JS.
 */
import dynamic from "next/dynamic";

import { useReportQuery } from "@/lib/reports/useReportQuery";
import { dimensionHeader, topNWithOther } from "@/lib/reports/series";
import type {
  CanvasFilters,
  PieWidget as PieWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import type { CsvCell } from "@/lib/reports/csv";

const PieWidgetChart = dynamic(() => import("./PieWidgetChart"), {
  ssr: false,
  loading: () => (
    <div
      data-testid="pie-widget-chart-loading"
      className="h-full w-full animate-pulse rounded bg-border/40"
    />
  ),
});

interface Props {
  widget: PieWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
}

export default function PieWidget({ widget, canvasFilters, editMode }: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const topN = widget.config.top_n ?? 8;
  const rawRows = (data?.rows ?? []).map((r) => ({
    label: String(r[dimensionKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));
  const rows = topNWithOther(rawRows, topN);

  // CSV export mirrors the displayed slices (after the top-N "Other"
  // roll-up): [dimension, measure].
  const measureLabel = widget.config.measure.field;
  const csvDataset = {
    headers: [dimensionHeader(dimensionKey), measureLabel],
    rows: rows.map((r) => [r.label, r.value]) as CsvCell[][],
  };

  return (
    <div
      data-testid="pie-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div
          className="text-sm font-semibold text-text-primary"
          aria-label={widget.title || "Pie chart"}
        >
          {widget.title || "Pie chart"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Pie chart"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid="pie-widget-loading"
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="pie-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : rows.length === 0 ? (
          <div
            data-testid="pie-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <PieWidgetChart rows={rows} />
        )}
      </div>
    </div>
  );
}
