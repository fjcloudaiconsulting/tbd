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

import { chartColor } from "@/lib/chart-colors";

const AREA_COLORS = [
  "var(--color-accent)",
  "var(--color-success)",
  "var(--color-info, var(--color-accent))",
  "var(--color-warning, var(--color-text-secondary))",
  "var(--color-danger)",
];

export interface AreaWidgetChartProps {
  rows: Array<{ label: string } & Record<string, number | string>>;
  seriesKeys: string[];
  labels: string[];
  stackId?: string;
}

export default function AreaWidgetChart({
  rows,
  seriesKeys,
  labels,
  stackId,
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
        <YAxis tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
        <Tooltip cursor={{ stroke: "var(--color-border)" }} />
        {seriesKeys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
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
  );
}
