"use client";

/**
 * Single-measure editor for the single-measure widget types (kpi, bar, pie,
 * sparkline).
 *
 * ⚠ ONE select, not Aggregation + Field (TBD-402). A source publishes
 * specific (agg, field) PAIRS; their cross product is not the catalog.
 * ``credit_utilization`` publishes only ``avg(utilization_pct)``, so an agg
 * select beside a field select could build ``sum(utilization_pct)`` — a 422
 * there, and on sources without a declared-agg guard something worse:
 * ``validate_against_catalog`` checks the FIELD and never the agg, so
 * ``avg(id)`` renders a plausible, meaningless number. Collapsing makes an
 * invalid pair unrepresentable instead of merely validated.
 */
import Section from "@/components/reports/config/Section";
import {
  AGG_HELP_KEY,
  UNSUPPORTED_MEASURE_KEY,
  UNSUPPORTED_MEASURE_NOTICE,
  measureFallbackLabel,
  measureSelectState,
  type MeasureOption,
} from "@/components/reports/config/controlConstants";
import type { Measure } from "@/lib/reports/types";

export default function SingleMeasureEditor({
  measure,
  onChange,
  measureOptions,
}: {
  measure: Measure;
  onChange: (m: Measure) => void;
  /**
   * The selected source's published measures as labelled options, in catalog
   * order. ``undefined`` while ``/sources`` is still loading — the select
   * then shows the current measure and is disabled, because offering a
   * stale fallback list is exactly how an invalid pair got chosen before.
   */
  measureOptions?: MeasureOption[];
}) {
  const sel = measureSelectState(
    measureOptions,
    measure,
    measureFallbackLabel(measure),
  );
  return (
    <Section label="Measure" help={AGG_HELP_KEY[measure.agg]}>
      <select
        value={sel.value}
        disabled={sel.disabled}
        onChange={(e) => {
          const opt = sel.options.find((o) => o.key === e.target.value);
          // The "(unsupported)" entry is inert: it exists to SHOW a legacy
          // pair, never to let one be re-selected.
          if (!opt || opt.key === UNSUPPORTED_MEASURE_KEY) return;
          onChange({ agg: opt.agg, field: opt.field });
        }}
        aria-label="Measure"
        className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary disabled:cursor-not-allowed disabled:opacity-60"
      >
        {sel.options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
      {sel.unsupported && (
        <p className="mt-1 text-[11px] leading-snug text-text-muted">
          {UNSUPPORTED_MEASURE_NOTICE}
        </p>
      )}
    </Section>
  );
}
