"use client";

/**
 * Recharts-rendering inner for SparklineWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a sparkline mounts. The
 * public SparklineWidget keeps all data wiring; this renders the trend
 * line for the already-prepared rows.
 */
import { Line, LineChart, ResponsiveContainer, Tooltip } from "recharts";

import { formatMeasureValue } from "@/lib/reports/series";

export interface SparklineWidgetChartProps {
  rows: Array<{ label: string; value: number }>;
  /** Display format for the measure value (tooltip only — sparkline has no axis). */
  format: "currency" | "number" | "percent";
}

export default function SparklineWidgetChart({
  rows,
  format,
}: SparklineWidgetChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={rows} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
        <Tooltip
          cursor={false}
          formatter={(v) => formatMeasureValue(Number(v), format)}
        />
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
  );
}
