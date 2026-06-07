"use client";

/**
 * Recharts-rendering inner for BarWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * BarWidget keeps all data wiring (simple vs sliced pivot, CSV, legend);
 * this renders the already-prepared rows.
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

export interface BarWidgetChartProps {
  rows: Array<Record<string, number | string>>;
  sliced: boolean;
  secondaryValues: string[];
  seriesKeys: string[];
}

export default function BarWidgetChart({
  rows,
  sliced,
  secondaryValues,
  seriesKeys,
}: BarWidgetChartProps) {
  return (
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
              key={seriesKeys[i]}
              dataKey={seriesKeys[i]}
              name={sv}
              stackId="stack"
              fill={categoricalColor(i)}
              radius={i === secondaryValues.length - 1 ? [4, 4, 0, 0] : 0}
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
  );
}
