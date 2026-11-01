/**
 * Pure date-preset range builders for the reports filter system.
 *
 * Lives in ``lib`` (not in the ``DatePresetChips`` component) so both the
 * component AND ``describe-filters.ts`` can import it without a lib →
 * component layering inversion. The component owns the UI; this module
 * owns the pure preset arithmetic.
 *
 * Presets resolve relative to the ``now`` passed in, producing an
 * absolute ISO ``{ start, end }`` window. That window is computed at
 * QUERY time (``resolveFilters``) and for display, never persisted: a
 * report stores the preset token (TBD-759), so "This month" saved in
 * October still means the current month in November.
 */
// ``PresetKey`` is owned by ``types.ts`` (so ``CanvasDateRange`` can
// carry ``preset`` without a type-only import cycle); re-export it here
// for the ~handful of importers that reach for it via this module.
import type {
  CalendarPresetKey,
  CanvasDateRange,
  PresetKey,
} from "@/lib/reports/types";

export type { CalendarPresetKey, PresetKey };

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

// ``next_cycle`` is a dynamic relative token (resolved server-side), so
// it has no calendar-computable window here — it is excluded from the
// ``Record`` key set alongside ``custom``.
export function buildPresetRanges(
  now: Date,
): Record<CalendarPresetKey, CanvasDateRange> {
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

export function isCalendarPreset(
  key: string | undefined,
): key is CalendarPresetKey {
  return (
    key === "this_month" ||
    key === "last_month" ||
    key === "ytd" ||
    key === "last_12_months"
  );
}

export function matchPreset(
  value: CanvasDateRange | undefined,
  ranges: Record<CalendarPresetKey, CanvasDateRange>,
): PresetKey | null {
  // A persisted token is the preset's identity: read it first, BEFORE the
  // empty-range early-return below (a token carries no start/end, so that
  // guard would otherwise return ``null``). Re-inferring from dates is what
  // made an October "This month" read as "Last month" in November.
  if (value?.preset) return value.preset;
  if (!value || (!value.start && !value.end)) return null;
  // Legacy absolute blob (saved before TBD-759, or a custom range): match
  // by exact dates against the windows for ``now``.
  for (const k of Object.keys(ranges) as Array<keyof typeof ranges>) {
    const r = ranges[k];
    if (r.start === value.start && r.end === value.end) return k;
  }
  return "custom";
}
