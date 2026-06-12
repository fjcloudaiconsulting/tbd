"use client";

/**
 * Recharts-rendering inner for PieWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * PieWidget keeps all data wiring (top-N roll-up, CSV); this renders the
 * already-prepared rows.
 */
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { formatMeasureValue } from "@/lib/reports/series";

// Canonical categorical chart palette (theme tokens, mirrors the
// dashboard donut). Slices cycle through chart-1..chart-5; the explicit
// "Other" roll-up below stays on the neutral border track.
const PIE_COLORS = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

export interface PieWidgetChartProps {
  rows: Array<{ label: string; value: number }>;
  /** Display format for the measure value (tooltip only — pie has no axis). */
  format: "currency" | "number" | "percent";
}

export default function PieWidgetChart({ rows, format }: PieWidgetChartProps) {
  return (
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
        <Tooltip formatter={(v) => formatMeasureValue(Number(v), format)} />
        <Legend
          verticalAlign="bottom"
          wrapperStyle={{ fontSize: 11 }}
          iconSize={8}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
