"use client";

/**
 * Bar widget — vertical bars over a primary dimension. Pulls rows
 * through ``useReportQuery``; treats the first dimension as the x-axis
 * label and ``value`` as the measure axis.
 *
 * When a SECONDARY dimension is set (``config.dimensions[1]``, e.g.
 * "account"), each total bar is sliced into stacked segments — one per
 * distinct secondary value, each a distinct color from the categorical
 * palette — with a legend mapping color → secondary value. The backend
 * AST supports up to two dimensions, so this is a single query grouped
 * by ``[primary, secondary]`` that we pivot client-side via
 * ``pivotBySecondaryDimension`` (reusing the same merge/backfill idiom
 * the multi-series widgets use). With no secondary dimension the widget
 * keeps its original single-color behavior.
 *
 * Recharts is the canvas chart engine across the app (Dashboard,
 * Budgets, Forecast Plans); reusing it here keeps visual register
 * consistent. The recharts subtree is code-split via ``next/dynamic``
 * (ssr:false) into ``BarWidgetChart`` so recharts loads only when a
 * chart mounts, keeping it out of the route's initial JS.
 */
import dynamic from "next/dynamic";
import { useMemo } from "react";

import { useReportQuery } from "@/lib/reports/useReportQuery";
import {
  dimensionHeader,
  measureFieldLabel,
  pivotBySecondaryDimension,
} from "@/lib/reports/series";
import type {
  BarWidget as BarWidgetType,
  CanvasFilters,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import type { CsvCell } from "@/lib/reports/csv";

const BarWidgetChart = dynamic(() => import("./BarWidgetChart"), {
  ssr: false,
  loading: () => (
    <div
      data-testid="bar-widget-chart-loading"
      className="h-full w-full animate-pulse rounded bg-border/40"
    />
  ),
});

interface Props {
  widget: BarWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

// Canonical categorical chart palette (theme tokens, mirrors the
// dashboard). Kept in sync with BarWidgetChart's bar fills; duplicated
// here rather than imported across the next/dynamic boundary so the
// legend doesn't pull the recharts-laden chart module into the route's
// initial JS.
const LEGEND_COLORS = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

function legendColor(index: number): string {
  return LEGEND_COLORS[index % LEGEND_COLORS.length];
}

export default function BarWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const primaryKey = widget.config.dimensions[0] ?? "dimension";
  const secondaryKey = widget.config.dimensions[1];
  const sliced = Boolean(secondaryKey);

  const queryRows = data?.rows ?? [];

  // Single-series shape (no break-down): one ``value`` per label.
  // Memoized on the query rows + primary key so unrelated parent renders
  // don't rebuild the array reference and force Recharts to re-layout.
  const simpleRows = useMemo(
    () =>
      queryRows.map((r) => ({
        label: String(r[primaryKey] ?? "—"),
        value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
      })),
    [queryRows, primaryKey],
  );

  // Sliced shape: pivot [primary, secondary] into one numeric field per
  // distinct secondary value so each becomes a stacked Recharts series.
  // Memoized like simpleRows so the O(n) pivot doesn't rerun (and force a
  // Recharts re-layout) on unrelated parent renders.
  const { rows: stackedRows, secondaryValues, seriesKeys } = useMemo(
    () =>
      sliced
        ? pivotBySecondaryDimension(queryRows, primaryKey, secondaryKey!)
        : { rows: [], secondaryValues: [] as string[], seriesKeys: [] as string[] },
    [sliced, queryRows, primaryKey, secondaryKey],
  );

  const rows = sliced ? stackedRows : simpleRows;
  const hasRows = rows.length > 0;
  const format = widget.config.format ?? "number";

  // CSV export. Single-series: [dimension, measure]. Sliced (break-down
  // by a secondary dimension): [primary dimension, ...one column per
  // secondary value], mirroring the stacked segments.
  const measureLabel = measureFieldLabel(widget.config.measure.field);
  const csvDataset = sliced
    ? {
        headers: [dimensionHeader(primaryKey), ...secondaryValues],
        rows: stackedRows.map((r) => [
          String(r.label),
          ...seriesKeys.map((sk) =>
            typeof r[sk] === "number" ? (r[sk] as number) : 0,
          ),
        ]) as CsvCell[][],
      }
    : {
        headers: [dimensionHeader(primaryKey), measureLabel],
        rows: simpleRows.map((r) => [r.label, r.value]) as CsvCell[][],
      };

  return (
    <div
      data-testid="bar-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-semibold text-text-primary">
          {widget.title || "Bar chart"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Bar chart"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid="bar-widget-loading"
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="bar-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : !hasRows ? (
          <div
            data-testid="bar-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <BarWidgetChart
            rows={rows}
            sliced={sliced}
            secondaryValues={secondaryValues}
            seriesKeys={seriesKeys}
            valueName={measureLabel}
            format={format}
            currency={currency}
          />
        )}
      </div>

      {/* DOM legend (outside the SVG) maps each color → secondary value.
          Rendered ourselves rather than via Recharts ``<Legend>`` so it
          stays visible in headless layouts (jsdom collapses the chart)
          and so swatch colors stay theme-token driven. */}
      {sliced && !isLoading && !error && hasRows && (
        <ul
          data-testid="bar-widget-legend"
          className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-text-secondary"
        >
          {secondaryValues.map((sv, i) => (
            <li
              key={sv}
              data-testid="bar-widget-legend-item"
              className="flex items-center gap-1"
            >
              <span
                data-testid="bar-widget-legend-swatch"
                data-color={legendColor(i)}
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 rounded-sm"
                style={{ backgroundColor: legendColor(i) }}
              />
              <span>{sv}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
