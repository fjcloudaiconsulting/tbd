"use client";

/**
 * Recharts-rendering inner for BarWidget. Split out so recharts is
 * dynamically imported (ssr:false) only when a chart mounts. The public
 * BarWidget keeps all data wiring (simple vs sliced pivot, CSV, legend);
 * this renders the already-prepared rows.
 *
 * TBD-382: this now backs BOTH the ``bar`` and ``stacked_bar`` widget
 * types. They differ by exactly one boolean — ``stacked`` — which flips
 * the break-down segments between stacked (one tall bar) and grouped
 * (side by side). ``stacked_bar`` no longer stacks MEASURES: across all
 * five report sources there is no pair of published measures whose sum
 * is meaningful, so its only stacking axis is the secondary dimension,
 * exactly like ``bar``'s "break down by".
 */
import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { SeriesTooltip } from "@/components/charts/SeriesTooltip";
import { chartColor } from "@/lib/chart-colors";
import { makeReportBarTooltipResolver } from "@/lib/reports/bar-tooltip";
import { formatMeasureValue } from "@/lib/reports/series";

export interface BarWidgetChartProps {
  rows: Array<Record<string, number | string>>;
  sliced: boolean;
  secondaryValues: string[];
  seriesKeys: string[];
  /**
   * Fill colour per slice, parallel to ``secondaryValues``. Supplied by
   * the caller rather than derived from the index here: after TBD-382 the
   * index comes from a STABLE alphabetical ordering of the secondary
   * label (so a category keeps its hue between loads), and the folded
   * "Other" bucket takes a NEUTRAL token that is outside the categorical
   * palette entirely. Recomputing ``CHART_SERIES[i % 8]`` here would undo
   * both.
   */
  sliceColors: string[];
  /**
   * When sliced, whether the break-down segments stack into one bar
   * (true) or sit side by side (false). Ignored when ``sliced`` is
   * false — a single-series bar has nothing to stack against.
   */
  stacked: boolean;
  /**
   * Human label for the single-series measure, surfaced as the bar's
   * tooltip ``name`` so hovering shows e.g. "Amount: 1234" instead of
   * the bare "value" dataKey. Sliced bars already carry per-segment
   * ``name`` from their secondary value.
   */
  valueName: string;
  /** Display format for the measure value (tooltip + value axis). */
  format: "currency" | "number" | "percent";
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function BarWidgetChart({
  rows,
  sliced,
  secondaryValues,
  seriesKeys,
  sliceColors,
  stacked,
  valueName,
  format,
  currency,
}: BarWidgetChartProps) {
  // Resolve each hovered series to its label + swatch, dropping backfilled-zero
  // breakdown segments so the tooltip only lists categories present in the
  // hovered bar (see lib/reports/bar-tooltip). The dep array is intentionally a
  // subset of the config: chartColor.spent is a module constant. Stacked vs
  // grouped does not change which series a hover resolves to, so ``stacked``
  // is deliberately NOT a dep.
  const resolveSeries = useMemo(
    () =>
      makeReportBarTooltipResolver({
        sliced,
        seriesKeys,
        secondaryValues,
        sliceColors,
        valueName,
        singleColor: chartColor.spent,
      }),
    [sliced, seriesKeys, secondaryValues, sliceColors, valueName],
  );

  const stackId = stacked ? "stack" : undefined;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
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
          cursor={{ fill: "var(--color-border)", opacity: 0.3 }}
          content={
            <SeriesTooltip
              resolve={resolveSeries}
              format={(v) => formatMeasureValue(v, format, currency)}
            />
          }
        />
        {sliced ? (
          secondaryValues.map((sv, i) => (
            <Bar
              key={seriesKeys[i]}
              dataKey={seriesKeys[i]}
              name={sv}
              stackId={stackId}
              fill={sliceColors[i]}
              // WCAG 2.2 AA 1.4.11: segment-vs-segment contrast measures
              // 1.05–1.59 across all 28 palette pairs, nowhere near 3:1.
              // Surface-vs-chart-N measures 3.13–9.48, so a 1px surface
              // stroke is what carries the boundary. Idiom already shipped
              // in PieWidgetChart.
              stroke="var(--color-surface)"
              strokeWidth={1}
              // Stacked: only the topmost segment gets the rounded cap so the
              // column reads as one bar. Grouped: every bar is its own column
              // and gets its own cap.
              radius={
                stackId && i !== secondaryValues.length - 1 ? 0 : [4, 4, 0, 0]
              }
              // Disabled so the whole reports family behaves alike: five of
              // the six charts in this directory already set this, and
              // BarWidgetChart was the lone outlier (TBD-382 R12).
              //
              // ⚠ This is a CONSISTENCY choice, not an accessibility one, and
              // the original rationale here said otherwise (TBD-428). It
              // claimed globals.css:382 cannot reach recharts' rAF loop and
              // that reduced-motion users therefore get motion. The mechanism
              // is right; the conclusion is not. recharts 3.x defaults
              // isAnimationActive to "auto", which resolves to
              // `!isSsr && !prefersReducedMotion` via its own
              // usePrefersReducedMotion hook — so omitting this prop already
              // honours the preference. Setting it false additionally removes
              // animation for users who did NOT opt out. It also credited
              // "react-smooth", which is not a dependency of this repo; that
              // code lives in recharts' own animation/ directory.
              // Fenced by tests/components/charts/reduced-motion.test.tsx.
              isAnimationActive={false}
            />
          ))
        ) : (
          <Bar
            dataKey="value"
            name={valueName}
            fill={chartColor.spent}
            stroke="var(--color-surface)"
            strokeWidth={1}
            radius={[4, 4, 0, 0]}
            isAnimationActive={false}
          />
        )}
      </BarChart>
    </ResponsiveContainer>
  );
}
