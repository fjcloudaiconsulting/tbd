"use client";

/**
 * Data tab of the widget editor: data source, measure(s), and the
 * primary/secondary dimension selects. Per-type sub-control visibility
 * lives here (it branches on ``widget.type``). All control logic is
 * extracted verbatim from the original widget config rail; mutations come
 * from ``buildWidgetMutations``.
 */
import Section from "@/components/reports/config/Section";
import SingleMeasureEditor from "@/components/reports/config/SingleMeasureEditor";
import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import {
  DIMENSION_OPTIONS,
  dimensionOptionsFor,
  isMultiSeries,
  measureFieldOptionsFor,
} from "@/components/reports/config/controlConstants";
import { dimensionHeader } from "@/lib/reports/series";
import { buildWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import { useReportSources } from "@/lib/reports/use-report-sources";
import type {
  BarConfig,
  Dataset,
  Dimension,
  KPIConfig,
  PieConfig,
  SankeyConfig,
  SparklineConfig,
  TableConfig,
  Widget,
} from "@/lib/reports/types";

/**
 * Pre-load fallback labels for the data-source select, used only while
 * the ``/sources`` catalog is still loading (so the control never renders
 * empty). Once the catalog resolves the select lists every source by its
 * own catalog label instead.
 */
const DATASET_FALLBACK_LABELS: Record<Dataset, string> = {
  transactions: "Transactions",
  accounts: "Accounts",
  recurring: "Recurring",
};

/**
 * Catalog-free dimension options: the static ``DIMENSION_OPTIONS`` plus
 * any of the widget's CURRENT dimension keys that aren't already in that
 * list (e.g. accounts-only ``account_type`` on a persisted accounts widget
 * loaded before ``/sources`` resolves). Ensures every controlled select
 * value has a matching option even without a catalog entry.
 */
function dimensionOptionsWithCurrent(
  current: Dimension[],
): Array<{ value: string; label: string }> {
  const out: Array<{ value: string; label: string }> = [...DIMENSION_OPTIONS];
  const seen = new Set(out.map((o) => o.value));
  for (const key of current) {
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ value: key, label: dimensionHeader(key) });
  }
  return out;
}

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
    setDataset,
  } = buildWidgetMutations(widget, onUpdate);

  const { sources } = useReportSources();
  // The catalog entry for the widget's current source. While the
  // catalog is still loading (``sources`` empty) this is undefined and
  // the pickers fall back to a catalog-free option set.
  const selected = sources.find((s) => s.key === widget.config.dataset);

  // Dimension options. When a catalog entry is known, narrow to its
  // dimensions. Otherwise (catalog still loading) fall back to the static
  // ``DIMENSION_OPTIONS`` UNIONED with the widget's current dimensions, so
  // a persisted accounts widget loaded before ``/sources`` resolves
  // doesn't render a select value (e.g. ``account_type``) with no matching
  // option — that mismatch trips React's "value not in options" warning.
  const currentDims = (
    (widget.config as { dimensions?: Dimension[] }).dimensions ?? []
  ) as Dimension[];
  const dimOptions = selected
    ? dimensionOptionsFor(selected)
    : dimensionOptionsWithCurrent(currentDims);

  // Field options narrowed to the selected source's published measures.
  // Undefined while the catalog loads → the editors fall back to the
  // static ``FIELD_OPTIONS``.
  const fieldOptions = selected ? measureFieldOptionsFor(selected) : undefined;

  function onSourceChange(key: string) {
    const entry = sources.find((s) => s.key === key);
    if (!entry) return; // unknown / not-yet-loaded source — no-op
    setDataset(key as Dataset, entry);
  }

  return (
    <>
      {/* Data source: Sankey is always transactions — hide the picker so
          users cannot accidentally switch it and 422 the fixed endpoint. */}
      {widget.type !== "sankey" && (
        <Section label="Data source">
          <select
            value={widget.config.dataset}
            onChange={(e) => onSourceChange(e.target.value)}
            aria-label="Data source"
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          >
            {sources.length > 0 ? (
              sources.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.label}
                </option>
              ))
            ) : (
              // Graceful fallback while the catalog loads: show the
              // widget's current source so the control never renders empty.
              <option value={widget.config.dataset}>
                {DATASET_FALLBACK_LABELS[widget.config.dataset] ?? "Transactions"}
              </option>
            )}
          </select>
        </Section>
      )}

      {/* Aggregation / measures section. Sankey has a fixed measure
          (transactions sum(amount)) so we skip this block for it. Single-
          measure widgets show one row; multi-series show one per series. */}
      {widget.type !== "sankey" && (
        isMultiSeries(widget) ? (
          <MeasuresEditor
            widget={widget}
            onChange={setSeries}
            fieldOptions={fieldOptions}
          />
        ) : (
          <SingleMeasureEditor
            measure={
              (widget.config as KPIConfig | BarConfig | PieConfig | SparklineConfig)
                .measure
            }
            onChange={setSingleMeasure}
            fieldOptions={fieldOptions}
          />
        )
      )}

      {/* Primary dimension: skip for kpi (no dimensions) and sankey (fixed schema). */}
      {widget.type !== "kpi" && widget.type !== "sankey" && (
        <Section label="Primary dimension" help="reports.master-category">
          <select
            value={
              ((widget.config as BarConfig).dimensions ?? [])[0] ?? "category"
            }
            onChange={(e) => setPrimaryDimension(e.target.value as Dimension)}
            aria-label="Primary dimension"
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          >
            {dimOptions.map((opt) => (
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
            {dimOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Section>
      )}

      {/* Sankey-specific knobs. Dataset + measure are fixed (transactions +
          sum(amount)); only granularity and top_n are user-configurable. */}
      {widget.type === "sankey" && (
        <>
          <Section label="Spending breakdown" help="reports.master-category">
            <fieldset className="flex flex-col gap-1.5">
              <legend className="sr-only">Spending breakdown level</legend>
              {(
                [
                  { value: "category", label: "Category" },
                  { value: "category_master", label: "Master category" },
                ] as const
              ).map(({ value, label }) => (
                <label
                  key={value}
                  className="flex items-center gap-2 text-sm text-text-primary"
                >
                  <input
                    type="radio"
                    name={`sankey-granularity-${widget.id}`}
                    value={value}
                    checked={
                      ((widget.config as SankeyConfig).spending_granularity ??
                        "category") === value
                    }
                    onChange={() => {
                      onUpdate({
                        ...widget,
                        config: {
                          ...(widget.config as SankeyConfig),
                          spending_granularity: value,
                        },
                      });
                    }}
                    aria-label={label}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </fieldset>
          </Section>
          <Section label="Top N categories">
            <input
              type="number"
              min={2}
              max={50}
              placeholder="All"
              value={(widget.config as SankeyConfig).top_n ?? ""}
              onChange={(e) => {
                const raw = e.target.value;
                onUpdate({
                  ...widget,
                  config: {
                    ...(widget.config as SankeyConfig),
                    top_n: raw === "" ? undefined : Number(raw) || undefined,
                  },
                });
              }}
              aria-label="Top N categories"
              className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
            />
          </Section>
        </>
      )}
    </>
  );
}
