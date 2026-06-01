"use client";

/**
 * Area widget — same data shape as Line, filled under the curve. When
 * multiple series are configured AND ``stacked`` is true, the areas
 * stack (each series sums on top of the prior). When ``stacked`` is
 * false, overlapping areas render with transparency.
 */
import {
  Area,
  AreaChart,
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
  AreaWidget as AreaWidgetType,
  CanvasFilters,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import { buildSeriesCsvDataset } from "./seriesCsv";

interface Props {
  widget: AreaWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
}

const AREA_COLORS = [
  "var(--color-accent)",
  "var(--color-success)",
  "var(--color-info, var(--color-accent))",
  "var(--color-warning, var(--color-text-secondary))",
  "var(--color-danger)",
];

export default function AreaWidget({ widget, canvasFilters, editMode }: Props) {
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
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
              <XAxis
                dataKey="label"
                tick={{ fill: chartColor.axisTick, fontSize: 11 }}
                interval={0}
              />
              <YAxis tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
              <Tooltip cursor={{ stroke: "var(--color-border)" }} />
              {seriesKeys.length > 1 && (
                <Legend wrapperStyle={{ fontSize: 11 }} />
              )}
              {seriesKeys.map((key, i) => (
                <Area
                  key={key}
                  type="monotone"
                  dataKey={key}
                  name={labels[i]}
                  stackId={stackId}
                  stroke={AREA_COLORS[i % AREA_COLORS.length]}
                  fill={AREA_COLORS[i % AREA_COLORS.length]}
                  fillOpacity={seriesKeys.length > 1 ? 0.35 : 0.55}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
