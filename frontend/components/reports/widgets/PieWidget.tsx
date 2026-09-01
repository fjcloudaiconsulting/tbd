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
import { widgetDataState } from "@/lib/reports/notices";
import { dimensionHeader, topNWithOther } from "@/lib/reports/series";
import { useWidgetFormat } from "@/lib/reports/widget-format";
import type {
  CanvasFilters,
  PieWidget as PieWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import WidgetNotices from "@/components/reports/WidgetNotices";
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
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function PieWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const { data, error, isLoading: dataLoading } = useReportQuery(widget, canvasFilters);

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const topN = widget.config.top_n ?? 8;
  const rawRows = (data?.rows ?? []).map((r) => ({
    label: String(r[dimensionKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));
  const rows = topNWithOther(rawRows, topN);
  // TBD-381: derived from the source catalog at render, never read from
  // config. `format` is no longer persisted -- see lib/reports/widget-format.ts.
  const { format: derivedFormat, isLoading: catalogLoading } = useWidgetFormat(widget.config.dataset, [widget.config.measure]);
  // Hold the skeleton until the catalog resolves: rendering an
  // unformatted value that then flips is worse than one more frame of
  // skeleton, and /query is in flight over the same window anyway.
  const isLoading = dataLoading || catalogLoading;
  const format = derivedFormat ?? "number";

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
            aria-label={widget.title || "Pie chart"}
          >
            {widget.title || "Pie chart"}
          </span>
          {/* LOUD on truncation: the donut total and `topNWithOther`'s
              "Other" slice are both composed from ACROSS the returned
              rows, so a short result makes them wrong rather than merely
              partial. The total is withheld (see `suppressTotal`); this
              notice is what explains why it is gone. */}
          <WidgetNotices
            metas={[data?.meta]}
            derivesCrossRowAggregate
            withholdsCrossRowAggregate
            widgetTitle={widget.title || "Pie chart"}
            state={widgetDataState(isLoading, error, rows.length > 0)}
          />
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
          <PieWidgetChart
            rows={rows}
            format={format}
            currency={currency}
            suppressTotal={!!data?.meta?.truncated}
          />
        )}
      </div>
    </div>
  );
}
