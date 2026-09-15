"use client";

/**
 * Recharts-rendering inner for LineWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a line chart mounts. The
 * public LineWidget keeps all data wiring; this renders merged rows.
 */
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { chartColor } from "@/lib/chart-colors";
import { formatMeasureValue } from "@/lib/reports/series";
import { useBalancesHidden } from "@/lib/hooks/use-org-currency";

export interface LineWidgetChartProps {
  rows: Array<{ label: string } & Record<string, number | string>>;
  seriesKeys: string[];
  labels: string[];
  /**
   * Stroke colour per series, parallel to ``seriesKeys``. Supplied by the
   * widget, which hands the SAME array to its DOM legend (TBD-427), so the
   * key and the plot cannot disagree. Recomputing ``CHART_SERIES[i % 8]``
   * here would re-open that drift.
   */
  seriesColors: string[];
  smooth?: boolean;
  /** Display format for the measure value (tooltip + value axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function LineWidgetChart({
  rows,
  seriesKeys,
  labels,
  seriesColors,
  smooth,
  format,
  currency,
}: LineWidgetChartProps) {
  useBalancesHidden(); // repaint on Hide balances (TBD-527)
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
        <XAxis
          dataKey="label"
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
          interval={0}
        />
        <YAxis
          // TBD-432: `width="auto"` lets recharts measure the widest rendered
          // tick (getCalculatedYAxisWidth) instead of reserving a fixed 92px.
          // The literal was sized for the widest formatted currency tick, so
          // it over-reserved on every narrower one: 5% of a `w:12` widget but
          // 27% at `w:4` and 66% at the grid minimum, where it left the plot
          // area a third of the card. Auto also cannot clip a LARGER value,
          // which the fixed width could.
          width="auto"
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
          tickFormatter={(v) => formatMeasureValue(Number(v), format, currency)}
        />
        <Tooltip
          cursor={{ stroke: "var(--color-border)" }}
          formatter={(v) => formatMeasureValue(Number(v), format, currency)}
        />
        {seriesKeys.map((key, i) => (
          <Line
            key={key}
            type={smooth === false ? "linear" : "monotone"}
            dataKey={key}
            name={labels[i]}
            stroke={seriesColors[i]}
            strokeWidth={2}
            dot={false}
            // recharts 3.8.1 bug: Line gates its dash on the raw prop, so 'auto' draws a stale, partial stroke under reduced motion. Off until TBD-528.
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
