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
import { widgetDataState } from "@/lib/reports/notices";
import { mergeSeriesRows, seriesLabel } from "@/lib/reports/series";
import { useWidgetFormat } from "@/lib/reports/widget-format";
import type {
  CanvasFilters,
  LineWidget as LineWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import WidgetNotices from "@/components/reports/WidgetNotices";
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
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function LineWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const measures = widget.config.measures.map((m) => m.measure);
  const { series, metas, isLoading: dataLoading, error } = useSeriesQueries(
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
      <div
        data-testid="widget-header"
        // ⚠ `pr-12` is not spacing taste. `WidgetShell`'s edit overlay is
        // absolutely positioned at `right-1 top-1` and occupies
        // x ∈ [W−52, W−4]; `WidgetCsvButton` renders `null` in edit mode,
        // so without this reservation the notice glyph lands at
        // x ∈ [W−42, W−16] and its top-right corner overlaps the REMOVE
        // control. See the geometry note in `WidgetNotices.tsx`.
        className={`mb-2 flex items-center justify-between gap-2${
          editMode ? " pr-12" : ""
        }`}
      >
        <div className="flex min-w-0 flex-1 items-center gap-1">
          <span
            className="min-w-0 truncate text-sm font-semibold text-text-primary"
            aria-label={widget.title || "Line chart"}
          >
            {widget.title || "Line chart"}
          </span>
          {/* Quiet: each point is its own group's own value; a short
              series is incomplete, not wrong. */}
          <WidgetNotices
            metas={metas}
            derivesCrossRowAggregate={false}
            withholdsCrossRowAggregate={false}
            widgetTitle={widget.title || "Line chart"}
            state={widgetDataState(isLoading, error, rows.length > 0)}
          />
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
            currency={currency}
          />
        )}
      </div>
    </div>
  );
}
