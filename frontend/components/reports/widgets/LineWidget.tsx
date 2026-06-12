"use client";

/**
 * Line widget — time-series over ``dimensions[0]``. Multiple series
 * are supported via ``config.measures`` (one entry per line). Each
 * series fires its own AST query and the rows are merged client-side
 * by the dimension key.
 *
 * Recharts is the canvas chart engine across the app (Dashboard,
 * Budgets, Forecast Plans); reusing it here keeps visual register
 * consistent. The recharts subtree is code-split via ``next/dynamic``
 * (ssr:false) into ``LineWidgetChart`` so it loads only when a chart
 * mounts, keeping recharts out of the route's initial JS.
 */
import dynamic from "next/dynamic";

import { useSeriesQueries } from "@/lib/reports/useReportQuery";
import { mergeSeriesRows, seriesLabel } from "@/lib/reports/series";
import type {
  CanvasFilters,
  LineWidget as LineWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import { buildSeriesCsvDataset } from "./seriesCsv";

const LineWidgetChart = dynamic(() => import("./LineWidgetChart"), {
  ssr: false,
  loading: () => (
    <div
      data-testid="line-widget-chart-loading"
      className="h-full w-full animate-pulse rounded bg-border/40"
    />
  ),
});

interface Props {
  widget: LineWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
}

export default function LineWidget({ widget, canvasFilters, editMode }: Props) {
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
  const csvDataset = buildSeriesCsvDataset(
    dimensionKey,
    rows,
    seriesKeys,
    labels,
  );

  return (
    <div
      data-testid="line-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div
          className="text-sm font-semibold text-text-primary"
          aria-label={widget.title || "Line chart"}
        >
          {widget.title || "Line chart"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Line chart"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid="line-widget-loading"
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="line-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : rows.length === 0 ? (
          <div
            data-testid="line-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <LineWidgetChart
            rows={rows}
            seriesKeys={seriesKeys}
            labels={labels}
            smooth={widget.config.smooth}
            format={format}
          />
        )}
      </div>
    </div>
  );
}
