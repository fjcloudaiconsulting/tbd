"use client";

import { label } from "@/lib/styles";

export type SeriesScope = "this" | "future";

const OPTIONS: { value: SeriesScope; label: string; hint: string }[] = [
  { value: "this", label: "Only this occurrence", hint: "The series keeps its amount and day." },
  {
    value: "future",
    label: "This and future occurrences",
    hint: "Also updates the series and its later pending occurrences. Paid ones stay as they are.",
  },
];

/**
 * TBD-756. Where a new amount or date on a pending recurring occurrence lands.
 * Nothing is pre-selected on purpose: the ticket rules out a silent default, so
 * the edit form refuses to save until one is picked. Rendered by the desktop
 * row and the mobile card; `idSuffix` keeps their radio groups apart.
 */
export function SeriesScopeChoice({
  idSuffix,
  value,
  onChange,
  legend = "Apply the new amount or date to",
  stacked = false,
}: {
  idSuffix: string;
  value: SeriesScope | null;
  onChange: (v: SeriesScope) => void;
  legend?: string;
  // One card per row at every width: the side-by-side break is keyed to the
  // viewport, which is wrong inside a narrow dialog.
  stacked?: boolean;
}) {
  return (
    <fieldset className="mt-2" data-testid={`edit-series-scope-${idSuffix}`}>
      <legend className={label}>{legend}</legend>
      <div className={`mt-1 grid grid-cols-1 gap-2 ${stacked ? "" : "md:grid-cols-2"}`}>
        {OPTIONS.map((o) => (
          <label
            key={o.value}
            className={`flex min-h-[44px] cursor-pointer items-start gap-3 rounded-md border px-3 py-2 transition-colors ${
              value === o.value ? "border-accent bg-accent-dim" : "border-border hover:border-border-strong"
            }`}
          >
            <input
              type="radio"
              name={`edit-series-scope-${idSuffix}`}
              value={o.value}
              checked={value === o.value}
              onChange={() => onChange(o.value)}
              className="mt-0.5 accent-accent"
            />
            <span>
              <span className="block text-sm font-medium text-text-primary">{o.label}</span>
              <span className="block text-xs text-text-secondary">{o.hint}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
