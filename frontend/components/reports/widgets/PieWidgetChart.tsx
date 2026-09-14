"use client";

/**
 * Recharts-rendering inner for PieWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * PieWidget keeps all data wiring (top-N roll-up, CSV); this renders the
 * already-prepared rows.
 */
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { formatMeasureValue } from "@/lib/reports/series";

export interface PieWidgetChartProps {
  rows: Array<{ label: string; value: number }>;
  /**
   * Fill per slice, parallel to ``rows``. Supplied by the widget, which hands
   * the SAME array to its DOM legend (TBD-427), and which paints the folded
   * "Other" with the neutral ``OTHER_COLOR`` (``--color-border-strong``).
   * The old ``--color-border`` measured 1.35:1 / 1.31:1 against the surface
   * and failed WCAG 1.4.11.
   */
  sliceColors: string[];
  /** Display format for the measure value (tooltip only — pie has no axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
  /**
   * Withhold the donut total (TBD-430).
   *
   * ⚠ Set under `meta.truncated`. `rows.reduce(...)` sums only the rows
   * that came back, and the sum was painted in the donut hole AND
   * exposed as an `sr-only` "Total: …" — so under truncation the WRONG
   * number was what assistive tech announced. Both go, together: dropping
   * only the visible one would leave the wrong figure audible and
   * invisible, which is worse. The header's loud notice explains it.
   */
  suppressTotal?: boolean;
}

export default function PieWidgetChart({
  rows,
  sliceColors,
  format,
  currency,
  suppressTotal,
}: PieWidgetChartProps) {
  // Self-guard: parent already ensures rows is non-empty, but be defensive.
  if (rows.length === 0) return null;

  const total = rows.reduce((sum, row) => sum + row.value, 0);
  const formattedTotal = formatMeasureValue(total, format, currency);

  return (
    <div className="relative h-full w-full">
      {!suppressTotal && (
        <>
          {/* Visually-hidden accessible alternative for the center total (SC 1.3.1).
              Must live OUTSIDE the aria-hidden overlay so screen readers find it. */}
          <span className="sr-only">Total: {formattedTotal}</span>
          {/* Center total — absolutely positioned over the donut hole */}
          <div
            className="pointer-events-none absolute inset-0 flex items-center justify-center"
            aria-hidden="true"
          >
            {/* Centred on the donut exactly: the legend lives outside this
                box (in PieWidget), so the box is the chart and `<Pie>`'s
                default cx/cy is its centre. */}
            <span
              className="text-sm font-bold text-text-primary"
              data-testid="pie-center-total"
            >
              {formattedTotal}
            </span>
          </div>
        </>
      )}
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={rows}
            dataKey="value"
            nameKey="label"
            innerRadius="58%"
            outerRadius="80%"
            stroke="var(--color-surface)"
            animationDuration={220}
          >
            {/* Index keys: labels are not unique (a real "Other" category
                beside the folded one). */}
            {rows.map((_, i) => (
              <Cell key={i} fill={sliceColors[i]} />
            ))}
          </Pie>
          <Tooltip
            formatter={(v) => formatMeasureValue(Number(v), format, currency)}
          />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}
