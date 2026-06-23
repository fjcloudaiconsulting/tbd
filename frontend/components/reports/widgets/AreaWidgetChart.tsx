"use client";

/**
 * Recharts-rendering inner for AreaWidget. Split out so the heavy
 * recharts bundle is dynamically imported (ssr:false) only when an area
 * chart actually mounts — keeping recharts out of the route's initial
 * JS. The public AreaWidget keeps all data wiring; this renders the
 * already-merged rows.
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

import { chartColor, CHART_SERIES } from "@/lib/chart-colors";
import { formatMeasureValue } from "@/lib/reports/series";

export interface AreaWidgetChartProps {
  rows: Array<{ label: string } & Record<string, number | string>>;
  seriesKeys: string[];
  labels: string[];
  stackId?: string;
  /** Display format for the measure value (tooltip + value axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
  /**
   * Stable widget id used to namespace SVG linearGradient ids so two area
   * widgets on the same canvas never share a <defs> id and steal each
   * other's gradient.
   */
  widgetId?: string;
}

export default function AreaWidgetChart({
  rows,
  seriesKeys,
  labels,
  stackId,
  format,
  currency,
  widgetId = "area",
}: AreaWidgetChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <defs>
          {seriesKeys.map((key, i) => {
            const color = CHART_SERIES[i % CHART_SERIES.length];
            // For overlaid multi-series, reduce fill density so lower series
            // remain legible behind upper ones. Stacked charts use a single
            // visual layer per series so the full 0.5 opacity is fine there.
            const topOpacity = seriesKeys.length > 1 && !stackId ? 0.35 : 0.5;
            return (
              <linearGradient
                key={key}
                id={`grad-${widgetId}-${i}`}
                x1="0"
                y1="0"
                x2="0"
                y2="1"
              >
                <stop offset="0%" stopColor={color} stopOpacity={topOpacity} />
                <stop offset="100%" stopColor={color} stopOpacity={0.02} />
              </linearGradient>
            );
          })}
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
        <XAxis
          dataKey="label"
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
          interval={0}
        />
        <YAxis
          width={92}
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
          tickFormatter={(v) => formatMeasureValue(Number(v), format, currency)}
        />
        <Tooltip
          cursor={{ stroke: "var(--color-border)" }}
          formatter={(v) => formatMeasureValue(Number(v), format, currency)}
        />
        {seriesKeys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {seriesKeys.map((key, i) => (
          <Area
            key={key}
            type="monotone"
            dataKey={key}
            name={labels[i]}
            stackId={stackId}
            stroke={CHART_SERIES[i % CHART_SERIES.length]}
            fill={`url(#grad-${widgetId}-${i})`}
            strokeWidth={2}
            isAnimationActive={false}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}
