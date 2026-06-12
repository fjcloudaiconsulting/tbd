"use client";

/**
 * Right-rail widget configuration UI. Slides in when a widget is
 * selected in edit mode. Mutations call back into the editor's
 * ``onUpdate`` so the report's full layout state stays the single
 * source of truth (debounced save handled at the editor level).
 *
 * The individual control blocks (Section, the measure editors, the
 * FilterEditor, and the mutation closures) now live under
 * ``components/reports/config`` so the anchored ``WidgetEditorPopover``
 * can reuse the exact same pieces. This rail renders them unchanged so
 * its external behaviour, markup, and every ``onUpdate`` payload stay
 * identical to before the extraction.
 *
 * Widget-type rules preserved here:
 *
 *  - All 8 v1 widget types via the ``WidgetType`` union.
 *  - Multi-series widgets (line / area / stacked bar / table) expose
 *    one aggregation row per entry in ``config.measures`` with an
 *    "Add series" button to grow the list. Table caps at 5 columns;
 *    series widgets cap at 5 too (visual register breaks past that).
 *  - Pie / Sparkline are locked to a single dimension + single
 *    aggregation (no add-series button).
 *  - Per-widget filter overrides go through the filter primitives
 *    (CategoryPicker, TagFilter, AccountFilter, DatePresetChips); the
 *    "Overrides canvas" pill fires when the widget value DIFFERS from
 *    the canvas value on the same field.
 */
import Section from "@/components/reports/config/Section";
import SingleMeasureEditor from "@/components/reports/config/SingleMeasureEditor";
import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import FilterEditor from "@/components/reports/config/FilterEditor";
import { isMultiSeries } from "@/components/reports/config/controlConstants";
import { useWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import { DIMENSION_OPTIONS } from "@/components/reports/config/controlConstants";
import type {
  BarConfig,
  CanvasFilters,
  Dimension,
  KPIConfig,
  PieConfig,
  SparklineConfig,
  StackedBarConfig,
  AreaConfig,
  TableConfig,
  Widget,
  WidgetFilters,
} from "@/lib/reports/types";

interface Props {
  widget: Widget;
  canvasFilters: CanvasFilters;
  onUpdate: (next: Widget) => void;
  onClose: () => void;
}

export default function ConfigRail({
  widget,
  canvasFilters,
  onUpdate,
  onClose,
}: Props) {
  const filters: WidgetFilters = widget.config.filters ?? {};

  const {
    setTitle,
    setFilters,
    setSingleMeasure,
    setSeries,
    setPrimaryDimension,
    setSecondaryDimension,
    setComparePrior,
    setTopN,
    setStacked,
  } = useWidgetMutations(widget, onUpdate);

  return (
    <aside
      data-testid="config-rail"
      className="flex h-full w-80 shrink-0 flex-col gap-4 overflow-y-auto border-l border-border bg-surface p-4"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-text-primary">
          Widget settings
        </h2>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-text-muted hover:text-text-primary"
        >
          Close
        </button>
      </div>

      <Section label="Title">
        <input
          type="text"
          value={widget.title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="Widget title"
          className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </Section>

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

      {widget.type === "kpi" && (
        <Section label="Compare to prior period">
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input
              type="checkbox"
              checked={Boolean(
                (widget.config as KPIConfig).compare_prior_period,
              )}
              onChange={(e) => setComparePrior(e.target.checked)}
              aria-label="Compare to prior period"
            />
            <span>Show delta vs prior period</span>
          </label>
        </Section>
      )}

      {widget.type === "pie" && (
        <Section label="Top N slices">
          <input
            type="number"
            min={2}
            max={20}
            value={(widget.config as PieConfig).top_n ?? 8}
            onChange={(e) => setTopN(Number(e.target.value) || 8)}
            aria-label="Top N slices"
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
        </Section>
      )}

      {(widget.type === "area" || widget.type === "stacked_bar") && (
        <Section label={widget.type === "stacked_bar" ? "Stack mode" : "Stack series"}>
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input
              type="checkbox"
              checked={
                widget.type === "stacked_bar"
                  ? (widget.config as StackedBarConfig).stacked !== false
                  : Boolean((widget.config as AreaConfig).stacked)
              }
              onChange={(e) => setStacked(e.target.checked)}
              aria-label="Stack series"
            />
            <span>Stack multiple series</span>
          </label>
        </Section>
      )}

      <FilterEditor
        filters={filters}
        canvasFilters={canvasFilters}
        onChange={setFilters}
      />
    </aside>
  );
}
