"use client";

/**
 * Date range filter — a row of preset chips above the absolute date
 * inputs. Clicking a chip stores the preset TOKEN (``{preset}``, no
 * start/end). Clicking "Custom" clears the chip selection and exposes
 * the absolute date inputs.
 *
 * Presets stay relative (TBD-759): a calendar token is resolved against
 * the local clock at query time by ``resolveFilters``, and ``next_cycle``
 * by the backend. Freezing the window at click time made a report saved
 * as "This month" read and query last month once the month rolled over.
 */
import { useMemo } from "react";

import type { CanvasDateRange } from "@/lib/reports/types";
import {
  buildPresetRanges,
  matchPreset,
  type PresetKey,
} from "@/lib/reports/date-presets";

// Re-export so existing importers of ``buildPresetRanges`` from this
// component keep working; the canonical home is now ``lib/reports/date-presets``.
export { buildPresetRanges } from "@/lib/reports/date-presets";

interface Props {
  value: CanvasDateRange | undefined;
  onChange: (next: CanvasDateRange | undefined) => void;
  /** Optional override for "today" — primarily for tests so a real
   * clock doesn't make the assertions wobble across the month flip. */
  now?: Date;
  /** Label prefix applied to aria-labels (e.g. "Canvas" vs "Widget"). */
  ariaPrefix?: string;
}

// ``title``/``ariaLabel`` override the accessible name where the visible
// chip text is too terse to be precise on its own. "Next cycle" reads as
// "Next billing cycle" for screen readers and on hover — the app's domain
// language is billing cycle, never "Next month".
const PRESETS: Array<{
  key: PresetKey;
  label: string;
  ariaLabel?: string;
}> = [
  { key: "this_month", label: "This month" },
  { key: "last_month", label: "Last month" },
  { key: "ytd", label: "YTD" },
  { key: "last_12_months", label: "Last 12 months" },
  {
    key: "next_cycle",
    label: "Next cycle",
    // Accessible name must CONTAIN the visible "Next cycle" (WCAG 2.5.3
    // Label in Name) so voice control matches, while still disambiguating.
    ariaLabel: "Next cycle (next billing cycle)",
  },
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

  // Detect which preset matches the current value (if any). A stored
  // token wins; a legacy absolute value matches by exact ISO equality on
  // start + end, and user-typed custom ranges fall through to "custom".
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
    // Every non-custom preset is a relative token — write ONLY the preset
    // marker (no absolute start/end). Calendar tokens resolve against the
    // local clock at query time (``resolveFilters``); ``next_cycle`` is
    // resolved by the backend per request.
    onChange({ preset: key });
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
              aria-label={p.ariaLabel}
              title={p.ariaLabel}
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
      {/* Native date inputs have a fixed intrinsic width and won't shrink, so
          below ~360px they overflow rather than wrap. Each label is full-width
          (stacked) on very narrow screens and auto-width (side by side) at
          ≥360px. */}
      {showDateInputs && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex w-full flex-col gap-1 min-[360px]:w-auto">
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
              className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
            />
          </label>
          <label className="flex w-full flex-col gap-1 min-[360px]:w-auto">
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
              className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
            />
          </label>
        </div>
      )}
    </div>
  );
}
