"use client";

/**
 * Bar widget — vertical bars over a single dimension. Pulls rows
 * through ``useReportQuery``; takes the first dimension key from
 * the widget config and treats ``value`` as the measure axis.
 *
 * Recharts is the canvas chart engine across the app (Dashboard,
 * Budgets, Forecast Plans); reusing it here keeps visual register
 * consistent.
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { chartColor } from "@/lib/chart-colors";
import { useReportQuery } from "@/lib/reports/useReportQuery";
import type {
  BarWidget as BarWidgetType,
  CanvasFilters,
} from "@/lib/reports/types";

interface Props {
  widget: BarWidgetType;
  canvasFilters?: CanvasFilters;
}

export default function BarWidget({ widget, canvasFilters }: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const rows = (data?.rows ?? []).map((r) => ({
    label: String(r[dimensionKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));

  return (
    <div
      data-testid="bar-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 text-sm font-semibold text-text-primary">
        {widget.title || "Bar chart"}
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
        ) : rows.length === 0 ? (
          <div
            data-testid="bar-widget-empty"
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
              <Bar
                dataKey="value"
                fill={chartColor.spent}
                radius={[4, 4, 0, 0]}
                animationDuration={400}
              />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
