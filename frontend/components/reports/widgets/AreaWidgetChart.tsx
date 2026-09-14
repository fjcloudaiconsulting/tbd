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
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { chartColor } from "@/lib/chart-colors";
import { formatMeasureValue } from "@/lib/reports/series";

export interface AreaWidgetChartProps {
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
  stackId?: string;
  /** Display format for the measure value (tooltip + value axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
  /**
   * Stable widget id used to namespace SVG linearGradient ids so two area
   * widgets on the same canvas never share a <defs> id and steal each
   * other's gradient.
   */
  widgetId?: string;
}

export default function AreaWidgetChart({
  rows,
  seriesKeys,
  labels,
  seriesColors,
  stackId,
  format,
  currency,
  widgetId = "area",
}: AreaWidgetChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <defs>
          {seriesKeys.map((key, i) => {
            const color = seriesColors[i];
            // For overlaid multi-series, reduce fill density so lower series
            // remain legible behind upper ones. Stacked charts use a single
            // visual layer per series so the full 0.5 opacity is fine there.
            const topOpacity = seriesKeys.length > 1 && !stackId ? 0.35 : 0.5;
            return (
              <linearGradient
                key={key}
                id={`grad-${widgetId}-${i}`}
                x1="0"
                y1="0"
                x2="0"
                y2="1"
              >
                <stop offset="0%" stopColor={color} stopOpacity={topOpacity} />
                <stop offset="100%" stopColor={color} stopOpacity={0.02} />
              </linearGradient>
            );
          })}
        </defs>
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
          <Area
            key={key}
            type="monotone"
            dataKey={key}
            name={labels[i]}
            stackId={stackId}
            stroke={seriesColors[i]}
            fill={`url(#grad-${widgetId}-${i})`}
            strokeWidth={2}
            isAnimationActive={false}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}
