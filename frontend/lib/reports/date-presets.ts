/**
 * Pure date-preset range builders for the reports filter system.
 *
 * Lives in ``lib`` (not in the ``DatePresetChips`` component) so both the
 * component AND ``describe-filters.ts`` can import it without a lib →
 * component layering inversion. The component owns the UI; this module
 * owns the pure preset arithmetic.
 *
 * Presets resolve relative to the ``now`` passed in, producing an
 * absolute ISO ``{ start, end }`` window. The architect-locked AST
 * doesn't model relative ranges; freezing the absolute window at
 * authoring time keeps the same report layout reproducible across
 * sessions until the user picks a new preset.
 */
import type { CanvasDateRange } from "@/lib/reports/types";

export type PresetKey =
  | "this_month"
  | "last_month"
  | "ytd"
  | "last_12_months"
  | "custom";

function isoDate(d: Date): string {
  // Build YYYY-MM-DD from local-clock components so a UTC-shifting
  // ``toISOString`` doesn't shove the date back by a day in negative-
  // offset timezones.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function endOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth() + 1, 0);
}

export function buildPresetRanges(
  now: Date,
): Record<Exclude<PresetKey, "custom">, CanvasDateRange> {
  const startThisMonth = startOfMonth(now);
  const endThisMonth = endOfMonth(now);

  const startLastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const endLastMonth = new Date(now.getFullYear(), now.getMonth(), 0);

  const startOfYear = new Date(now.getFullYear(), 0, 1);

  const startLast12 = new Date(now.getFullYear() - 1, now.getMonth(), 1);

  return {
    this_month: { start: isoDate(startThisMonth), end: isoDate(endThisMonth) },
    last_month: { start: isoDate(startLastMonth), end: isoDate(endLastMonth) },
    ytd: { start: isoDate(startOfYear), end: isoDate(now) },
    last_12_months: { start: isoDate(startLast12), end: isoDate(now) },
  };
}

export function matchPreset(
  value: CanvasDateRange | undefined,
  ranges: Record<Exclude<PresetKey, "custom">, CanvasDateRange>,
): PresetKey | null {
  if (!value || (!value.start && !value.end)) return null;
  for (const k of Object.keys(ranges) as Array<keyof typeof ranges>) {
    const r = ranges[k];
    if (r.start === value.start && r.end === value.end) return k;
  }
  return "custom";
}
