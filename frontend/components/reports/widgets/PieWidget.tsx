"use client";

/**
 * Pie widget — share-of-total over a single dimension. The spec caps
 * the visible slice count: anything beyond ``top_n`` (default 8) is
 * rolled into a single "Other" slice. Legend renders below the pie.
 *
 * Single dimension, single aggregation — the config rail locks both
 * to length 1 when the widget type is ``pie``.
 */
import {
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

import { useReportQuery } from "@/lib/reports/useReportQuery";
import { topNWithOther } from "@/lib/reports/series";
import type {
  CanvasFilters,
  PieWidget as PieWidgetType,
} from "@/lib/reports/types";

interface Props {
  widget: PieWidgetType;
  canvasFilters?: CanvasFilters;
}

const PIE_COLORS = [
  "var(--color-accent)",
  "var(--color-success)",
  "var(--color-info, var(--color-accent))",
  "var(--color-warning, var(--color-text-secondary))",
  "var(--color-danger)",
  "var(--color-text-secondary)",
  "var(--color-border)",
  "var(--color-text-muted)",
  "var(--color-bg-elevated, var(--color-border))",
];

export default function PieWidget({ widget, canvasFilters }: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const topN = widget.config.top_n ?? 8;
  const rawRows = (data?.rows ?? []).map((r) => ({
    label: String(r[dimensionKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));
  const rows = topNWithOther(rawRows, topN);

  return (
    <div
      data-testid="pie-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div
        className="mb-2 text-sm font-semibold text-text-primary"
        aria-label={widget.title || "Pie chart"}
      >
        {widget.title || "Pie chart"}
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
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={rows}
                dataKey="value"
                nameKey="label"
                innerRadius="40%"
                outerRadius="75%"
                stroke="var(--color-surface)"
                isAnimationActive={false}
              >
                {rows.map((row, i) => (
                  <Cell
                    key={row.label}
                    fill={
                      row.label === "Other"
                        ? "var(--color-border)"
                        : PIE_COLORS[i % PIE_COLORS.length]
                    }
                  />
                ))}
              </Pie>
              <Tooltip />
              <Legend
                verticalAlign="bottom"
                wrapperStyle={{ fontSize: 11 }}
                iconSize={8}
              />
            </PieChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
