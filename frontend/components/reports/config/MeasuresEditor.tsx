"use client";

/**
 * Multi-series measure editor (one row per ``config.measures`` entry, with
 * add/remove and a per-type cap) for line / area / table.
 *
 * ``stacked_bar`` no longer uses this: TBD-382 made it a single-measure
 * widget whose only stacking axis is ``dimensions[1]``. It still PERSISTS
 * ``config.measures`` as a length-1 array (the backend model requires it),
 * so it stays inside ``isMultiSeries`` — flipping that guard would route its
 * writes through ``setSingleMeasure`` and ``config.measure``, which 422s on
 * the next save.
 *
 * TBD-382 R7 — "+ Add series" seeds the next unused catalog (agg, field)
 * PAIR, and DISABLES itself when the catalog's pairs are exhausted. The
 * shipped bug seeded ``{agg:"sum", field: fields[0]}``: series 1 usually
 * already was that pair, so the new series drew pixel-identical on top of
 * the old one.
 *
 * ⚠ There is deliberately NO agg-rotation fallback when the pairs run out.
 * `validate_against_catalog` is not the only backend validator
 * (`CreditUtilizationSource` enforces the PAIR against an exhaustive
 * `_DECLARED_AGG` map, so a rotated pair 422s the whole widget); rotation
 * mints meaningless measures like ``COALESCE(SUM(transactions.id), 0)``,
 * formatted as a plain number by the field-only backstop — a new
 * silent-wrong-number vector introduced by the fix; and on ``networth``,
 * whose ``build_rows`` ignores ``measure.agg``/``measure.field`` entirely,
 * it reproduces Defect B verbatim. A control that refuses is honest.
 */
import HelpTooltip from "@/components/help/HelpTooltip";
import {
  AGG_HELP_KEY,
  MAX_SERIES,
  MAX_TABLE_COLUMNS,
  UNSUPPORTED_MEASURE_KEY,
  UNSUPPORTED_MEASURE_NOTICE,
  measureFallbackLabel,
  measureSelectState,
  nextUnusedMeasurePair,
  type MeasureOption,
} from "@/components/reports/config/controlConstants";
import type {
  AreaConfig,
  LineConfig,
  Measure,
  SeriesConfig,
  StackedBarConfig,
  TableConfig,
  Widget,
} from "@/lib/reports/types";

export default function MeasuresEditor({
  widget,
  onChange,
  measureOptions,
  measurePairs,
}: {
  widget: Widget & { config: LineConfig | AreaConfig | StackedBarConfig | TableConfig };
  onChange: (m: SeriesConfig[]) => void;
  /**
   * The selected source's published measures as labelled options, in catalog
   * order (TBD-402). ``undefined`` while ``/sources`` is still loading — the
   * select then shows the current measure and is disabled, because offering
   * a stale fallback list is exactly how an invalid pair got chosen before.
   */
  measureOptions?: MeasureOption[];
  /**
   * The selected source's published measures as (agg, field) PAIRS, in
   * catalog order. ``undefined`` while ``/sources`` is still loading — R7
   * is defined in terms of catalog pairs and has no meaning before they
   * exist, so the add button stays disabled over that window.
   */
  measurePairs?: Measure[];
}) {
  const measures = widget.config.measures;
  const cap = widget.type === "table" ? MAX_TABLE_COLUMNS : MAX_SERIES;
  const nextPair = nextUnusedMeasurePair(measurePairs, measures);
  // "Unknown" and "exhausted" are different states and only one of them
  // earns an explanation: before the catalog resolves there is nothing to
  // say, so the button is merely inert.
  const catalogResolved = measurePairs !== undefined;
  const exhausted = catalogResolved && nextPair === undefined;
  const addDisabled = !catalogResolved || nextPair === undefined;

  function update(idx: number, next: SeriesConfig) {
    const copy = [...measures];
    copy[idx] = next;
    onChange(copy);
  }

  function add() {
    if (measures.length >= cap || !nextPair) return;
    onChange([...measures, { measure: nextPair }]);
  }

  function remove(idx: number) {
    if (measures.length <= 1) return;
    onChange(measures.filter((_, i) => i !== idx));
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        {widget.type === "table" ? "Columns" : "Series"}
      </div>
      {measures.map((s, idx) => {
        const sel = measureSelectState(
          measureOptions,
          s.measure,
          measureFallbackLabel(s.measure),
        );
        return (
        <div
          key={idx}
          data-testid={`measure-row-${idx}`}
          className="flex flex-col gap-1 rounded-md border border-border bg-bg p-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-muted">
              {widget.type === "table" ? `Column ${idx + 1}` : `Series ${idx + 1}`}
            </span>
            {measures.length > 1 && (
              <button
                type="button"
                data-testid={`measure-remove-${idx}`}
                onClick={() => remove(idx)}
                className="text-xs text-text-muted hover:text-danger"
                aria-label={`Remove ${widget.type === "table" ? "column" : "series"} ${idx + 1}`}
              >
                Remove
              </button>
            )}
          </div>
          <input
            type="text"
            value={s.label ?? ""}
            onChange={(e) =>
              update(idx, { ...s, label: e.target.value || undefined })
            }
            placeholder={
              widget.type === "table" ? "Column label" : "Series label"
            }
            aria-label={`Series ${idx + 1} label`}
            className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
          />
          <div className="flex items-center gap-1">
            <select
              value={sel.value}
              disabled={sel.disabled}
              onChange={(e) => {
                const opt = sel.options.find((o) => o.key === e.target.value);
                // The "(unsupported)" entry is inert: it exists to SHOW a
                // legacy pair, never to let one be re-selected.
                if (!opt || opt.key === UNSUPPORTED_MEASURE_KEY) return;
                update(idx, {
                  ...s,
                  measure: { agg: opt.agg, field: opt.field },
                });
              }}
              aria-label={`Series ${idx + 1} measure`}
              className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary disabled:cursor-not-allowed disabled:opacity-60"
            >
              {sel.options.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
            {/* Per-agg explainer, keyed off the SELECTED row's agg — the
                content is per-aggregation, not per-select, so it survives
                the two selects collapsing into one. */}
            <HelpTooltip k={AGG_HELP_KEY[s.measure.agg]} />
          </div>
          {sel.unsupported && (
            <p className="text-[11px] leading-snug text-text-muted">
              {UNSUPPORTED_MEASURE_NOTICE}
            </p>
          )}
        </div>
        );
      })}
      {measures.length < cap && (
        <div className="flex items-center gap-1">
          <button
            type="button"
            data-testid="measure-add"
            onClick={add}
            disabled={addDisabled}
            // DESIGN.md's Pressable-Surfaces Rule requires a visible Brass
            // Tally focus state on anything pressable; this button shipped
            // with `hover:` only. Disabled treatment reuses the shipped
            // primitive from lib/styles.ts.
            className="rounded-md border border-dashed border-border px-2 py-1 text-xs text-text-secondary transition hover:border-accent hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-60"
          >
            + Add {widget.type === "table" ? "column" : "series"}
          </button>
          {exhausted && (
            // The reason renders at normal contrast OUTSIDE the dimmed
            // control, through HelpTooltip — not as a `title` attribute
            // (not keyboard-reachable, not reliably announced).
            <span data-testid="measure-add-exhausted-help">
              <HelpTooltip k="reports.series.exhausted" />
            </span>
          )}
        </div>
      )}
    </div>
  );
}
