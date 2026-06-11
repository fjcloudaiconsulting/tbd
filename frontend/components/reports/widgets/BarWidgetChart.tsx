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

import { chartColor } from "@/lib/chart-colors";

// Canonical categorical chart palette (theme tokens, mirrors the
// dashboard). chart-5 (danger/red) sits last so neutral break-down
// segments don't pick up alarm semantics until the cycle wraps. Kept in
// sync with the legend swatches in BarWidget (palette duplicated rather
// than imported across the next/dynamic boundary so the legend doesn't
// pull this recharts-laden module into the route's initial JS).
const BAR_SLICE_COLORS = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

function barSliceColor(index: number): string {
  return BAR_SLICE_COLORS[index % BAR_SLICE_COLORS.length];
}

export interface BarWidgetChartProps {
  rows: Array<Record<string, number | string>>;
  sliced: boolean;
  secondaryValues: string[];
  seriesKeys: string[];
  /**
   * Human label for the single-series measure, surfaced as the bar's
   * tooltip ``name`` so hovering shows e.g. "Amount: 1234" instead of
   * the bare "value" dataKey. Sliced bars already carry per-segment
   * ``name`` from their secondary value.
   */
  valueName: string;
}

export default function BarWidgetChart({
  rows,
  sliced,
  secondaryValues,
  seriesKeys,
  valueName,
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
              fill={barSliceColor(i)}
              radius={i === secondaryValues.length - 1 ? [4, 4, 0, 0] : 0}
              animationDuration={220}
            />
          ))
        ) : (
          <Bar
            dataKey="value"
            name={valueName}
            fill={chartColor.spent}
            radius={[4, 4, 0, 0]}
            animationDuration={220}
          />
        )}
      </BarChart>
    </ResponsiveContainer>
  );
}
