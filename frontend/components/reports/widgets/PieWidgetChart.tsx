"use client";

/**
 * Recharts-rendering inner for PieWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * PieWidget keeps all data wiring (top-N roll-up, CSV); this renders the
 * already-prepared rows.
 */
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { CHART_SERIES } from "@/lib/chart-colors";
import { formatMeasureValue } from "@/lib/reports/series";

export interface PieWidgetChartProps {
  rows: Array<{ label: string; value: number }>;
  /** Display format for the measure value (tooltip only — pie has no axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function PieWidgetChart({
  rows,
  format,
  currency,
}: PieWidgetChartProps) {
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  const formattedTotal = formatMeasureValue(total, format, currency);

  return (
    <div className="relative h-full w-full">
      {/* Center total — absolutely positioned over the donut hole */}
      <div
        className="pointer-events-none absolute inset-0 flex items-center justify-center"
        aria-hidden="true"
      >
        {/* Offset upward slightly to clear the legend at the bottom */}
        <span
          className="-mt-8 text-sm font-bold text-[var(--color-text-primary)]"
          data-testid="pie-center-total"
        >
          {formattedTotal}
        </span>
      </div>
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={rows}
            dataKey="value"
            nameKey="label"
            innerRadius="58%"
            outerRadius="80%"
            stroke="var(--color-surface)"
            isAnimationActive={false}
          >
            {rows.map((row, i) => (
              <Cell
                key={row.label}
                fill={
                  row.label === "Other"
                    ? "var(--color-border)"
                    : CHART_SERIES[i % CHART_SERIES.length]
                }
              />
            ))}
          </Pie>
          <Tooltip
            formatter={(v) => formatMeasureValue(Number(v), format, currency)}
          />
          <Legend
            verticalAlign="bottom"
            wrapperStyle={{ fontSize: 11 }}
            iconSize={8}
          />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}
