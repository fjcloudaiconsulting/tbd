"use client";

/**
 * Recharts-rendering inner for PieWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * PieWidget keeps all data wiring (top-N roll-up, CSV); this renders the
 * already-prepared rows.
 */
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

const PIE_COLORS = [
  "var(--color-accent)",
  "var(--color-success)",
  "var(--color-info, var(--color-accent))",
  "var(--color-warning, var(--color-text-secondary))",
  "var(--color-danger)",
  "var(--color-text-secondary)",
  "var(--color-border)",
  "var(--color-text-muted)",
  "var(--color-surface-overlay)",
];

export interface PieWidgetChartProps {
  rows: Array<{ label: string; value: number }>;
}

export default function PieWidgetChart({ rows }: PieWidgetChartProps) {
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
        <Tooltip />
        <Legend
          verticalAlign="bottom"
          wrapperStyle={{ fontSize: 11 }}
          iconSize={8}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
