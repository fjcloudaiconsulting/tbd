"use client";

/**
 * Recharts-rendering inner for StackedBarWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * StackedBarWidget keeps all data wiring; this renders merged rows.
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
import { formatMeasureValue } from "@/lib/reports/series";

// Canonical categorical chart palette (theme tokens, mirrors the
// dashboard). chart-5 (danger/red) sits last so neutral series don't
// pick up alarm semantics until the cycle wraps.
const BAR_COLORS = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

export interface StackedBarWidgetChartProps {
  rows: Array<{ label: string } & Record<string, number | string>>;
  seriesKeys: string[];
  labels: string[];
  stackId?: string;
  /** Display format for the measure value (tooltip + value axis). */
  format: "currency" | "number" | "percent";
}

export default function StackedBarWidgetChart({
  rows,
  seriesKeys,
  labels,
  stackId,
  format,
}: StackedBarWidgetChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
        <XAxis
          dataKey="label"
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
          interval={0}
        />
        <YAxis
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
          tickFormatter={(v) => formatMeasureValue(Number(v), format)}
        />
        <Tooltip
          cursor={{ fill: "var(--color-border)", opacity: 0.3 }}
          formatter={(v) => formatMeasureValue(Number(v), format)}
        />
        {seriesKeys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
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
  );
}
