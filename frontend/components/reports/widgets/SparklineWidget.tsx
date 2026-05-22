"use client";

/**
 * Sparkline widget — compact trend line designed for a small grid
 * slot. No axes, no legend, no axis rulers. Tooltip on hover. The
 * widget surfaces the LAST data point as a big number alongside the
 * trend line so a single glance gives both the latest value and the
 * direction of travel.
 *
 * Single dimension, single aggregation — the config rail locks both
 * when the widget type is ``sparkline``.
 */
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

import { useReportQuery } from "@/lib/reports/useReportQuery";
import type {
  CanvasFilters,
  SparklineWidget as SparklineWidgetType,
} from "@/lib/reports/types";

interface Props {
  widget: SparklineWidgetType;
  canvasFilters?: CanvasFilters;
}

export default function SparklineWidget({ widget, canvasFilters }: Props) {
  const { data, error, isLoading } = useReportQuery(widget, canvasFilters);

  const dimensionKey = widget.config.dimensions[0] ?? "dimension";
  const rows = (data?.rows ?? []).map((r) => ({
    label: String(r[dimensionKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));
  const lastValue = rows.length > 0 ? rows[rows.length - 1].value : null;
  const format = widget.config.format ?? "number";

  return (
    <div
      data-testid="sparkline-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col justify-center gap-1 rounded-lg border border-border bg-surface p-3"
      aria-label={widget.title || "Sparkline"}
    >
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        {widget.title || "Sparkline"}
      </div>
      {isLoading ? (
        <div
          data-testid="sparkline-widget-loading"
          className="h-6 w-20 animate-pulse rounded bg-border"
        />
      ) : error ? (
        <div
          role="alert"
          data-testid="sparkline-widget-error"
          className="text-sm text-danger"
        >
          Couldn&apos;t load
        </div>
      ) : rows.length === 0 ? (
        <div
          data-testid="sparkline-widget-empty"
          className="text-2xl font-semibold text-text-muted"
        >—</div>
      ) : (
        <>
          <div
            data-testid="sparkline-widget-value"
            className="text-xl font-semibold text-text-primary"
          >
            {formatValue(lastValue ?? 0, format)}
          </div>
          <div className="-mx-1 h-10">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rows} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
                <Tooltip cursor={false} />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="var(--color-accent)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  );
}

function formatValue(value: number, format: "currency" | "number" | "percent") {
  if (format === "currency") {
    return value.toLocaleString(undefined, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0,
    });
  }
  if (format === "percent") {
    return `${value.toFixed(1)}%`;
  }
  return value.toLocaleString();
}
