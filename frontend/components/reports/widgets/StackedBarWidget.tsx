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
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { chartColor } from "@/lib/chart-colors";
import { useSeriesQueries } from "@/lib/reports/useReportQuery";
import { mergeSeriesRows, seriesLabel } from "@/lib/reports/series";
import type {
  CanvasFilters,
  StackedBarWidget as StackedBarWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import { buildSeriesCsvDataset } from "./seriesCsv";

interface Props {
  widget: StackedBarWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
}

const BAR_COLORS = [
  "var(--color-accent)",
  "var(--color-success)",
  "var(--color-info, var(--color-accent))",
  "var(--color-warning, var(--color-text-secondary))",
  "var(--color-danger)",
];

export default function StackedBarWidget({
  widget,
  canvasFilters,
  editMode,
}: Props) {
  const measures = widget.config.measures.map((m) => m.measure);
  const { series, isLoading, error } = useSeriesQueries(
    widget,
    canvasFilters,
    measures,
  );

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
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
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis
                dataKey="label"
                tick={{ fill: chartColor.axisTick, fontSize: 11 }}
                interval={0}
              />
              <YAxis tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
              <Tooltip cursor={{ fill: "var(--color-border)", opacity: 0.3 }} />
              {seriesKeys.length > 1 && (
                <Legend wrapperStyle={{ fontSize: 11 }} />
              )}
              {seriesKeys.map((key, i) => (
                <Bar
                  key={key}
                  dataKey={key}
                  name={labels[i]}
                  stackId={stackId}
                  fill={BAR_COLORS[i % BAR_COLORS.length]}
                  radius={
                    stackId && i === seriesKeys.length - 1
                      ? [4, 4, 0, 0]
                      : stackId
                        ? 0
                        : [4, 4, 0, 0]
                  }
                  isAnimationActive={false}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
