/**
 * Tooltip series resolver for the report Bar widget, consumed by the shared
 * `SeriesTooltip` (components/charts/SeriesTooltip).
 *
 * Fixes the breakdown-tooltip bug: when a bar is sliced by a secondary
 * dimension (e.g. primary = Category Group, breakdown = Category), the pivot
 * (`pivotBySecondaryDimension`) backfills every globally-distinct secondary
 * value with 0 so recharts can stack. recharts' default tooltip then lists
 * ALL of them, surfacing categories that don't belong to the hovered group.
 * Dropping zero-value series here means a bar's tooltip shows only the
 * categories that actually have data for that bar — mirroring how the
 * forecast/budget charts omit zero-height stacked segments.
 */
import type {
  SeriesTooltipEntry,
  TooltipSeries,
} from "@/components/charts/SeriesTooltip";

export interface ReportBarTooltipConfig {
  /** True when the bar is stacked by a secondary dimension. */
  sliced: boolean;
  /** Generated dataKeys for each secondary value (parallel to the next two). */
  seriesKeys: string[];
  /** Human label per secondary value. */
  secondaryValues: string[];
  /** Swatch colour per secondary value (theme tokens). */
  sliceColors: string[];
  /** Label for the single (non-sliced) value bar. */
  valueName: string;
  /** Swatch colour for the single value bar. */
  singleColor: string;
}

export function makeReportBarTooltipResolver(
  cfg: ReportBarTooltipConfig,
): (entry: SeriesTooltipEntry) => TooltipSeries | null {
  if (!cfg.sliced) {
    // Single bar: one "value" series, always shown (a measured 0 is real,
    // not a backfill artifact).
    return (entry) =>
      entry.dataKey === "value"
        ? { label: cfg.valueName, color: cfg.singleColor }
        : null;
  }

  const byKey = new Map<string, TooltipSeries>();
  cfg.seriesKeys.forEach((key, i) => {
    byKey.set(key, {
      label: cfg.secondaryValues[i],
      color: cfg.sliceColors[i],
    });
  });

  return (entry) => {
    // Drop zero-value segments so the tooltip shows only series with data for
    // the hovered bar. This catches the pivot's backfilled zeros (the bug) and
    // also a segment that genuinely nets to exactly 0 — but that segment is
    // zero-height and invisible in the bar, so omitting it from the tooltip is
    // consistent with what's drawn (same rule the forecast/budget charts use).
    // NOTE the asymmetry with the single-bar branch above, which keeps a
    // measured 0: there the bar IS that one value, so its row is never noise.
    if (Number(entry.value) === 0) return null;
    if (entry.dataKey == null) return null;
    return byKey.get(String(entry.dataKey)) ?? null;
  };
}
