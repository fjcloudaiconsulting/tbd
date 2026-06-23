"use client";

/**
 * Area widget — same data shape as Line, filled under the curve. When
 * multiple series are configured AND ``stacked`` is true, the areas
 * stack (each series sums on top of the prior). When ``stacked`` is
 * false, overlapping areas render with transparency.
 *
 * The recharts-rendering subtree is code-split: it lives in
 * ``AreaWidgetChart`` and is loaded via ``next/dynamic`` (ssr:false) so
 * the ~100KB recharts bundle is fetched only when a chart actually
 * mounts, not in the route's initial JS. The fallback matches the
 * existing loading placeholder (the global prefers-reduced-motion block
 * neutralizes the pulse).
 */
import dynamic from "next/dynamic";

import { useSeriesQueries } from "@/lib/reports/useReportQuery";
import { mergeSeriesRows, seriesLabel } from "@/lib/reports/series";
import type {
  AreaWidget as AreaWidgetType,
  CanvasFilters,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import { buildSeriesCsvDataset } from "./seriesCsv";

const AreaWidgetChart = dynamic(() => import("./AreaWidgetChart"), {
  ssr: false,
  loading: () => (
    <div
      data-testid="area-widget-chart-loading"
      className="h-full w-full animate-pulse rounded bg-border/40"
    />
  ),
});

interface Props {
  widget: AreaWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function AreaWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const measures = widget.config.measures.map((m) => m.measure);
  const { series, isLoading, error } = useSeriesQueries(
    widget,
    canvasFilters,
    measures,
  );

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const format = widget.config.format ?? "number";
  const seriesKeys = widget.config.measures.map((_, i) => `s${i}`);
  const rows = mergeSeriesRows(series, dimensionKey, seriesKeys);
  const labels = widget.config.measures.map((m, i) =>
    seriesLabel(m, i, widget.config.measures.length),
  );
  const stackId = widget.config.stacked && seriesKeys.length > 1 ? "stack" : undefined;
  const csvDataset = buildSeriesCsvDataset(
    dimensionKey,
    rows,
    seriesKeys,
    labels,
  );

  return (
    <div
      data-testid="area-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div
          className="text-sm font-semibold text-text-primary"
          aria-label={widget.title || "Area chart"}
        >
          {widget.title || "Area chart"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Area chart"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid="area-widget-loading"
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="area-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : rows.length === 0 ? (
          <div
            data-testid="area-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <AreaWidgetChart
            rows={rows}
            seriesKeys={seriesKeys}
            labels={labels}
            stackId={stackId}
            format={format}
            currency={currency}
            widgetId={widget.id}
          />
        )}
      </div>
    </div>
  );
}
