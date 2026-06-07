"use client";

/**
 * Date range filter — a row of preset chips above the absolute date
 * inputs. Clicking a chip fills both ``from`` and ``to`` with the
 * resolved ISO date range. Clicking "Custom" clears the chip
 * selection and exposes the absolute date inputs.
 *
 * Presets resolved relative to the local clock at click time so the
 * stored value is an absolute window. The architect-locked AST
 * doesn't model relative ranges; freezing the absolute window at
 * authoring time keeps the same report layout reproducible across
 * sessions until the user picks a new preset.
 */
import { useMemo } from "react";

import type { CanvasDateRange } from "@/lib/reports/types";

interface Props {
  value: CanvasDateRange | undefined;
  onChange: (next: CanvasDateRange | undefined) => void;
  /** Optional override for "today" — primarily for tests so a real
   * clock doesn't make the assertions wobble across the month flip. */
  now?: Date;
  /** Label prefix applied to aria-labels (e.g. "Canvas" vs "Widget"). */
  ariaPrefix?: string;
}

type PresetKey = "this_month" | "last_month" | "ytd" | "last_12_months" | "custom";

const PRESETS: Array<{ key: PresetKey; label: string }> = [
  { key: "this_month", label: "This month" },
  { key: "last_month", label: "Last month" },
  { key: "ytd", label: "YTD" },
  { key: "last_12_months", label: "Last 12 months" },
  { key: "custom", label: "Custom" },
];

export default function DatePresetChips({
  value,
  onChange,
  now,
  ariaPrefix = "Date",
}: Props) {
  // ``todayKey`` is a stable YYYY-MM-DD-ish string that only changes
  // when the calendar day flips (or the test override changes). The
  // original code passed ``today.toDateString()`` directly as a useMemo
  // dep, which fails react-hooks/exhaustive-deps because (a) the
  // linter rejects method calls as stable deps and (b) the expression
  // is recomputed on every render. Pinning the string in its own
  // variable and listing both inputs (string + raw ``now`` reference)
  // keeps the memo stable AND silences the lint rule cleanly.
  const todayKey = (now ?? new Date()).toDateString();
  const presetRanges = useMemo(
    () => buildPresetRanges(now ?? new Date(todayKey)),
    [now, todayKey],
  );

  // Detect which preset matches the current value (if any). The check
  // is exact ISO equality on start + end; user-typed custom ranges fall
  // through to "custom".
  const activePreset = matchPreset(value, presetRanges);
  const showDateInputs = activePreset === "custom" || (!activePreset && value);

  function pick(key: PresetKey) {
    if (key === "custom") {
      // Don't clobber the user's typed dates if they had them; just
      // keep the current value but flag it as custom.
      if (!value) {
        onChange({});
      }
      return;
    }
    onChange(presetRanges[key]);
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        data-testid="date-preset-chips"
        className="flex flex-wrap gap-1.5"
      >
        {PRESETS.map((p) => {
          const isActive = activePreset === p.key;
          return (
            <button
              key={p.key}
              type="button"
              data-testid={`date-preset-${p.key}`}
              aria-pressed={isActive}
              onClick={() => pick(p.key)}
              className={`rounded-full border px-3 py-1 text-xs transition ${
                isActive
                  ? "border-accent bg-accent text-accent-text"
                  : "border-border text-text-secondary hover:bg-surface-raised"
              }`}
            >
              {p.label}
            </button>
          );
        })}
      </div>
      {showDateInputs && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase tracking-wider text-text-muted">
              From
            </span>
            <input
              type="date"
              data-testid="date-preset-from"
              aria-label={`${ariaPrefix} date from`}
              value={value?.start ?? ""}
              onChange={(e) =>
                onChange({
                  ...(value ?? {}),
                  start: e.target.value || undefined,
                })
              }
              className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase tracking-wider text-text-muted">
              To
            </span>
            <input
              type="date"
              data-testid="date-preset-to"
              aria-label={`${ariaPrefix} date to`}
              value={value?.end ?? ""}
              onChange={(e) =>
                onChange({
                  ...(value ?? {}),
                  end: e.target.value || undefined,
                })
              }
              className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
            />
          </label>
        </div>
      )}
    </div>
  );
}

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

function matchPreset(
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
