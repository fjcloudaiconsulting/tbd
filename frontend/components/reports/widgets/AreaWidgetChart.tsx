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
}

export default function AreaWidgetChart({
  rows,
  seriesKeys,
  labels,
  stackId,
  format,
  currency,
}: AreaWidgetChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
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
            fill={CHART_SERIES[i % CHART_SERIES.length]}
            fillOpacity={seriesKeys.length > 1 ? 0.35 : 0.55}
            strokeWidth={2}
            isAnimationActive={false}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}
