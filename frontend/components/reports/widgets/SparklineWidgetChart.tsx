"use client";

/**
 * Recharts-rendering inner for SparklineWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a sparkline mounts. The
 * public SparklineWidget keeps all data wiring; this renders the trend
 * line for the already-prepared rows.
 */
import { Line, LineChart, ResponsiveContainer, Tooltip } from "recharts";

import { formatMeasureValue } from "@/lib/reports/series";
import { useBalancesHidden } from "@/lib/hooks/use-org-currency";

export interface SparklineWidgetChartProps {
  rows: Array<{ label: string; value: number }>;
  /** Display format for the measure value (tooltip only — sparkline has no axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function SparklineWidgetChart({
  rows,
  format,
  currency,
}: SparklineWidgetChartProps) {
  useBalancesHidden(); // repaint on Hide balances (TBD-527)
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={rows} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
        <Tooltip
          cursor={false}
          formatter={(v) => formatMeasureValue(Number(v), format, currency)}
        />
        <Line
          type="monotone"
          dataKey="value"
          stroke="var(--color-accent)"
          strokeWidth={2}
          dot={false}
          // recharts 3.8.1 bug: Line gates its dash on the raw prop, so 'auto' draws a stale, partial stroke under reduced motion. Off until TBD-528.
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
