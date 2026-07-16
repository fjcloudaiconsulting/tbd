"use client";

/**
 * Sankey widget — directed cash-flow diagram from income sources through
 * spending categories. Uses @nivo/sankey (a dedicated flow chart library)
 * via ``SankeyWidgetChart``, code-split with ``next/dynamic`` (ssr:false)
 * to keep Nivo out of the route's initial JS.
 *
 * Data comes from ``useSankeyQuery`` which targets the dedicated
 * ``POST /api/v1/reports/query/sankey`` endpoint. The backend only returns
 * links when there is income in the period (the Sankey is income → spending
 * by design), so an empty ``links`` array means "no income" and is surfaced
 * as a contextual empty-state message rather than the generic "No data".
 */
import dynamic from "next/dynamic";

import WidgetCsvButton from "./WidgetCsvButton";
import { useSankeyQuery } from "@/lib/reports/useSankeyQuery";
import { sankeyNodeLabel } from "@/lib/reports/sankey-labels";
import type { CsvCell } from "@/lib/reports/csv";
import type {
  CanvasFilters,
  SankeyWidget as SankeyWidgetType,
} from "@/lib/reports/types";

const SankeyWidgetChart = dynamic(() => import("./SankeyWidgetChart"), {
  ssr: false,
  loading: () => (
    <div
      data-testid="sankey-widget-chart-loading"
      className="h-full w-full animate-pulse rounded bg-border/40"
    />
  ),
});

interface Props {
  widget: SankeyWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  currency?: string;
}

export default function SankeyWidget({ widget, canvasFilters, editMode, currency }: Props) {
  const { data, error, isLoading } = useSankeyQuery(widget, canvasFilters);

  const links = data?.links ?? [];
  const hasLinks = links.length > 0;

  // The flow rows the diagram renders, one row per source → target link.
  // Source/target run through the same hub-label map the chart uses so the CSV
  // shows "Income" rather than the raw "__hub_income__" sentinel. Value is left
  // raw-numeric (consistent with every other widget's CSV).
  const csvDataset = {
    headers: ["Source", "Target", "Amount"],
    rows: links.map((l) => [
      sankeyNodeLabel(l.source),
      sankeyNodeLabel(l.target),
      l.value,
    ]) as CsvCell[][],
  };

  return (
    <div
      data-testid="sankey-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div
        className="mb-2 flex items-center justify-between gap-2"
        aria-label={widget.title || "Cash flow Sankey diagram"}
      >
        <div className="text-sm font-semibold text-text-primary">
          {widget.title || "Cash flow"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Cash flow"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid="sankey-widget-loading"
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="sankey-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : !hasLinks ? (
          <div
            data-testid="sankey-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No income in this period to chart cash flow
          </div>
        ) : (
          <SankeyWidgetChart
            links={links}
            currency={currency}
            title={widget.title}
          />
        )}
      </div>
    </div>
  );
}
