"use client";

/**
 * Data tab of the widget editor: data source, measure(s), and the
 * primary/secondary dimension selects. Per-type sub-control visibility
 * lives here (it branches on ``widget.type``). All control logic is
 * extracted verbatim from ``ConfigRail``; mutations come from
 * ``useWidgetMutations``.
 */
import Section from "@/components/reports/config/Section";
import SingleMeasureEditor from "@/components/reports/config/SingleMeasureEditor";
import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import {
  DIMENSION_OPTIONS,
  isMultiSeries,
} from "@/components/reports/config/controlConstants";
import { useWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import type {
  BarConfig,
  Dimension,
  KPIConfig,
  PieConfig,
  SparklineConfig,
  TableConfig,
  Widget,
} from "@/lib/reports/types";

export default function DataTab({
  widget,
  onUpdate,
}: {
  widget: Widget;
  onUpdate: (next: Widget) => void;
}) {
  const {
    setSingleMeasure,
    setSeries,
    setPrimaryDimension,
    setSecondaryDimension,
  } = useWidgetMutations(widget, onUpdate);

  return (
    <>
      <Section label="Data source">
        <select
          disabled
          value={widget.config.dataset}
          aria-label="Data source"
          className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-muted"
        >
          <option value="transactions">Transactions</option>
        </select>
      </Section>

      {/* Aggregation / measures section. Single-measure widgets show
          one row; multi-series widgets show one row per series with
          an "Add series" button at the bottom. */}
      {isMultiSeries(widget) ? (
        <MeasuresEditor
          widget={widget}
          onChange={setSeries}
        />
      ) : (
        <SingleMeasureEditor
          measure={
            (widget.config as KPIConfig | BarConfig | PieConfig | SparklineConfig)
              .measure
          }
          onChange={setSingleMeasure}
        />
      )}

      {widget.type !== "kpi" && (
        <Section label="Primary dimension" help="reports.master-category">
          <select
            value={
              ((widget.config as BarConfig).dimensions ?? [])[0] ?? "category"
            }
            onChange={(e) => setPrimaryDimension(e.target.value as Dimension)}
            aria-label="Primary dimension"
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          >
            {DIMENSION_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Section>
      )}

      {/* Secondary dimension picker. For a bar widget this "Break down
          by" slices each total bar into stacked segments (one color per
          secondary value, e.g. per account) with a legend. For a table
          it adds a second grouping column. Both consume
          ``dimensions[1]`` and the backend AST already supports two
          dimensions, so no query-layer change is needed. */}
      {(widget.type === "bar" || widget.type === "table") && (
        <Section
          label={
            widget.type === "bar"
              ? "Break down by (optional)"
              : "Secondary dimension (optional)"
          }
          help="reports.master-category"
        >
          <select
            value={
              ((widget.config as BarConfig | TableConfig).dimensions ?? [])[1] ??
              ""
            }
            onChange={(e) =>
              setSecondaryDimension((e.target.value || "") as Dimension | "")
            }
            aria-label={
              widget.type === "bar" ? "Break down by" : "Secondary dimension"
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          >
            <option value="">None</option>
            {DIMENSION_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Section>
      )}
    </>
  );
}
