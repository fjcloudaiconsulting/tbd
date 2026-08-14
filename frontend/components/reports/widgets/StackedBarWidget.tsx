"use client";

/**
 * Stacked bar widget — extends the bar pattern with ``stackId`` on
 * every bar so multiple series stack on top of one another. Up to two
 * dimensions are allowed (the AST hard cap from PR1); the first
 * becomes the x-axis label and each entry in ``config.measures``
 * becomes a stacked series.
 *
 * With a single measure this falls back to a plain bar visual; the
 * config rail still lets the user add additional series.
 *
 * The recharts subtree is code-split via ``next/dynamic`` (ssr:false)
 * into ``StackedBarWidgetChart`` so recharts loads only when a chart
 * mounts, keeping it out of the route's initial JS.
 */
import dynamic from "next/dynamic";

import { useSeriesQueries } from "@/lib/reports/useReportQuery";
import { mergeSeriesRows, seriesLabel } from "@/lib/reports/series";
import { useWidgetFormat } from "@/lib/reports/widget-format";
import type {
  CanvasFilters,
  StackedBarWidget as StackedBarWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import { buildSeriesCsvDataset } from "./seriesCsv";

const StackedBarWidgetChart = dynamic(
  () => import("./StackedBarWidgetChart"),
  {
    ssr: false,
    loading: () => (
      <div
        data-testid="stacked-bar-widget-chart-loading"
        className="h-full w-full animate-pulse rounded bg-border/40"
      />
    ),
  },
);

interface Props {
  widget: StackedBarWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function StackedBarWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const measures = widget.config.measures.map((m) => m.measure);
  const { series, isLoading: dataLoading, error } = useSeriesQueries(
    widget,
    canvasFilters,
    measures,
  );

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  // TBD-381: one derived format for the shared Y axis. Series with
  // differing formats fall to "number" rather than stamping series[0]'s
  // unit on a scale the others do not share.
  const { format: derivedFormat, isLoading: catalogLoading } = useWidgetFormat(
    widget.config.dataset,
    widget.config.measures.map((m) => m.measure),
  );
  // Hold the skeleton until the catalog resolves: rendering an
  // unformatted value that then flips is worse than one more frame of
  // skeleton, and /query is in flight over the same window anyway.
  const isLoading = dataLoading || catalogLoading;
  const format = derivedFormat ?? "number";
  const seriesKeys = widget.config.measures.map((_, i) => `s${i}`);
  const rows = mergeSeriesRows(series, dimensionKey, seriesKeys);
  const labels = widget.config.measures.map((m, i) =>
    seriesLabel(m, i, widget.config.measures.length),
  );
  // Default to stacking unless the user explicitly turned it off.
  const stackId =
    widget.config.stacked === false || seriesKeys.length < 2
      ? undefined
      : "stack";
  const csvDataset = buildSeriesCsvDataset(
    dimensionKey,
    rows,
    seriesKeys,
    labels,
  );

  return (
    <div
      data-testid="stacked-bar-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div
          className="text-sm font-semibold text-text-primary"
          aria-label={widget.title || "Stacked bar chart"}
        >
          {widget.title || "Stacked bar chart"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Stacked bar chart"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid="stacked-bar-widget-loading"
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="stacked-bar-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : rows.length === 0 ? (
          <div
            data-testid="stacked-bar-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <StackedBarWidgetChart
            rows={rows}
            seriesKeys={seriesKeys}
            labels={labels}
            stackId={stackId}
            format={format}
            currency={currency}
          />
        )}
      </div>
    </div>
  );
}
