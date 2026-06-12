/**
 * Series resolvers for the shared `SeriesTooltip` on the forecast/budget
 * bar charts. Centralised here (rather than inline in each chart) so the
 * four charts stay in sync and the colour-tier logic is unit-tested.
 *
 * The swatch colour must MATCH the bar the user is hovering, including the
 * dynamic per-row fills: forecast `actual` turns red when it exceeds
 * `planned`; budget `spent` walks the utilisation tiers (gold → watch →
 * over) exactly like its `<Cell>` fill. Zero-height stacked segments
 * (e.g. `over` on an on-budget row) are omitted so the tooltip doesn't
 * show noise like "Over budget: $0.00".
 */
import { chartColor } from "@/lib/chart-colors";
import type {
  SeriesTooltipEntry,
  TooltipSeries,
} from "@/components/charts/SeriesTooltip";

/** Planned-vs-actual charts. `actual` is red when it exceeds `planned`. */
export function resolveForecastSeries(
  entry: SeriesTooltipEntry,
): TooltipSeries | null {
  if (entry.dataKey === "planned") {
    return { label: "Planned", color: chartColor.planned };
  }
  if (entry.dataKey === "actual") {
    const row = entry.payload as
      | { planned?: number; actual?: number }
      | undefined;
    const over = row ? Number(row.actual) > Number(row.planned) : false;
    return { label: "Actual", color: over ? chartColor.over : chartColor.actual };
  }
  return null;
}

/**
 * Budget charts (dashboard "Budget Progress" + budgets page overview).
 * The `spent` swatch tracks the utilisation tier so it matches the bar's
 * per-row `<Cell>` fill; zero-value stacked segments are dropped.
 */
export function resolveBudgetSeries(
  entry: SeriesTooltipEntry,
): TooltipSeries | null {
  if (Number(entry.value) === 0) return null; // omit zero-height segments
  if (entry.dataKey === "spent") {
    const pct = budgetPercentUsed(entry.payload);
    return {
      label: "Spent",
      color:
        pct > 100
          ? chartColor.over
          : pct > 80
            ? chartColor.watch
            : chartColor.spent,
    };
  }
  if (entry.dataKey === "over") {
    return { label: "Over budget", color: chartColor.over };
  }
  if (entry.dataKey === "remaining") {
    return { label: "Remaining", color: chartColor.remaining };
  }
  return null;
}

/**
 * Resolve a row's utilisation %. The dashboard datum carries `pct`
 * (percent_used) directly; the budgets-page datum doesn't, so derive it:
 * an `over` amount means >100%, otherwise spent / (spent + remaining).
 */
function budgetPercentUsed(payload: SeriesTooltipEntry["payload"]): number {
  const row = payload as
    | { pct?: number; spent?: number; remaining?: number; over?: number }
    | undefined;
  if (row?.pct != null) return Number(row.pct);
  if (Number(row?.over) > 0) return 101;
  const spent = Number(row?.spent ?? 0);
  const remaining = Number(row?.remaining ?? 0);
  const total = spent + remaining;
  return total > 0 ? (spent / total) * 100 : 0;
}
