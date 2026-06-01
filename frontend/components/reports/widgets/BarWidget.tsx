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

import { categoricalColor, chartColor } from "@/lib/chart-colors";
import { useReportQuery } from "@/lib/reports/useReportQuery";
import { pivotBySecondaryDimension } from "@/lib/reports/series";
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

  const primaryKey = widget.config.dimensions[0] ?? "dimension";
  const secondaryKey = widget.config.dimensions[1];
  const sliced = Boolean(secondaryKey);

  const queryRows = data?.rows ?? [];

  // Single-series shape (no break-down): one ``value`` per label.
  const simpleRows = queryRows.map((r) => ({
    label: String(r[primaryKey] ?? "—"),
    value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
  }));

  // Sliced shape: pivot [primary, secondary] into one numeric field per
  // distinct secondary value so each becomes a stacked Recharts series.
  const { rows: stackedRows, secondaryValues } = sliced
    ? pivotBySecondaryDimension(queryRows, primaryKey, secondaryKey!)
    : { rows: [], secondaryValues: [] as string[] };

  const rows = sliced ? stackedRows : simpleRows;
  const hasRows = rows.length > 0;

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
        ) : !hasRows ? (
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
              {sliced ? (
                secondaryValues.map((sv, i) => (
                  <Bar
                    key={sv}
                    dataKey={sv}
                    name={sv}
                    stackId="stack"
                    fill={categoricalColor(i)}
                    radius={
                      i === secondaryValues.length - 1 ? [4, 4, 0, 0] : 0
                    }
                    animationDuration={400}
                  />
                ))
              ) : (
                <Bar
                  dataKey="value"
                  fill={chartColor.spent}
                  radius={[4, 4, 0, 0]}
                  animationDuration={400}
                />
              )}
            </BarChart>
          </ResponsiveContainer>
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
                data-color={categoricalColor(i)}
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 rounded-sm"
                style={{ backgroundColor: categoricalColor(i) }}
              />
              <span>{sv}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
