"use client";

/**
 * Single-measure editor (Aggregation + Field) for the single-measure
 * widget types (kpi, bar, pie, sparkline). Extracted verbatim from the
 * original widget config rail.
 */
import Section from "@/components/reports/config/Section";
import {
  AGG_HELP_KEY,
  AGG_OPTIONS,
  FIELD_OPTIONS,
} from "@/components/reports/config/controlConstants";
import type { Aggregation, Measure, MeasureField } from "@/lib/reports/types";

export default function SingleMeasureEditor({
  measure,
  onChange,
  fieldOptions,
}: {
  measure: Measure;
  onChange: (m: Measure) => void;
  /**
   * Field options narrowed to the selected data source's published
   * measures. When omitted (catalog not yet loaded), falls back to the
   * static ``FIELD_OPTIONS`` so the transactions path and existing tests
   * are unchanged.
   */
  fieldOptions?: Array<{ value: string; label: string }>;
}) {
  const fields = fieldOptions ?? FIELD_OPTIONS;
  return (
    <>
      <Section label="Aggregation" help={AGG_HELP_KEY[measure.agg]}>
        <select
          value={measure.agg}
          onChange={(e) =>
            onChange({ ...measure, agg: e.target.value as Aggregation })
          }
          aria-label="Aggregation"
          className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        >
          {AGG_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </Section>
      <Section label="Field">
        <select
          value={measure.field}
          onChange={(e) =>
            onChange({ ...measure, field: e.target.value as MeasureField })
          }
          aria-label="Field"
          className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        >
          {fields.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </Section>
    </>
  );
}
