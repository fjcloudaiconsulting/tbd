"use client";

/**
 * Right-rail widget configuration UI. Slides in when a widget is
 * selected in edit mode. Mutations call back into the editor's
 * ``onUpdate`` so the report's full layout state stays the single
 * source of truth (debounced save handled at the editor level).
 *
 * PR3 expands this rail to support:
 *
 *  - All 8 v1 widget types via the ``WidgetType`` union.
 *  - Multi-series widgets (line / area / stacked bar / table) expose
 *    one aggregation row per entry in ``config.measures`` with an
 *    "Add series" button to grow the list. Table caps at 5 columns;
 *    series widgets cap at 5 too (visual register breaks past that).
 *  - Pie / Sparkline are locked to a single dimension + single
 *    aggregation (no add-series button).
 *  - Per-widget filter overrides go through the new filter
 *    primitives (CategoryPicker, TagFilter, AccountFilter,
 *    DatePresetChips); the "Overrides canvas" pill from PR2 still
 *    fires when the widget value DIFFERS from the canvas value on
 *    the same field.
 */
import { useId } from "react";

import AccountFilter from "@/components/reports/filters/AccountFilter";
import CategoryPicker from "@/components/reports/filters/CategoryPicker";
import DatePresetChips from "@/components/reports/filters/DatePresetChips";
import TagFilter from "@/components/reports/filters/TagFilter";
import HelpTooltip from "@/components/help/HelpTooltip";
import type { HelpTooltipKey } from "@/lib/help/tooltips";
import type {
  AreaConfig,
  Aggregation,
  BarConfig,
  CanvasFilters,
  Dimension,
  KPIConfig,
  LineConfig,
  Measure,
  MeasureField,
  PieConfig,
  SeriesConfig,
  SparklineConfig,
  StackedBarConfig,
  TableConfig,
  TagMatch,
  Widget,
  WidgetFilters,
} from "@/lib/reports/types";
import { isFieldOverridden } from "@/lib/reports/resolve";

interface Props {
  widget: Widget;
  canvasFilters: CanvasFilters;
  onUpdate: (next: Widget) => void;
  onClose: () => void;
}

const AGG_OPTIONS: Array<{ value: Aggregation; label: string }> = [
  { value: "sum", label: "Sum" },
  { value: "count", label: "Count" },
  { value: "avg", label: "Average" },
  { value: "distinct", label: "Distinct count" },
];

/** Tooltip key for each aggregation type (plain-language explainer). */
const AGG_HELP_KEY: Record<Aggregation, HelpTooltipKey> = {
  sum: "reports.agg.sum",
  count: "reports.agg.count",
  avg: "reports.agg.avg",
  distinct: "reports.agg.distinct",
};

const FIELD_OPTIONS: Array<{ value: MeasureField; label: string }> = [
  { value: "amount", label: "Amount" },
  { value: "id", label: "Row count (id)" },
  { value: "category_id", label: "Category" },
  { value: "account_id", label: "Account" },
];

const DIMENSION_OPTIONS: Array<{ value: Dimension; label: string }> = [
  { value: "category", label: "Category" },
  { value: "category_master", label: "Master category" },
  { value: "account", label: "Account" },
  { value: "tag", label: "Tag" },
  { value: "txn_type", label: "Transaction type" },
  { value: "status", label: "Status" },
  { value: "month", label: "Month" },
  { value: "week", label: "Week" },
  { value: "day", label: "Day" },
];

const MAX_SERIES = 5;
const MAX_TABLE_COLUMNS = 5;

/** Widget types that carry ``config.measures`` (multi-series). */
function isMultiSeries(
  w: Widget,
): w is Widget & { config: LineConfig | AreaConfig | StackedBarConfig | TableConfig } {
  return (
    w.type === "line" ||
    w.type === "area" ||
    w.type === "stacked_bar" ||
    w.type === "table"
  );
}

/** Widget types locked to a single dimension + single aggregation. */
function isSingleAggLocked(w: Widget): boolean {
  return w.type === "pie" || w.type === "sparkline";
}

export default function ConfigRail({
  widget,
  canvasFilters,
  onUpdate,
  onClose,
}: Props) {
  const filters: WidgetFilters = widget.config.filters ?? {};

  function setTitle(title: string) {
    onUpdate({ ...widget, title });
  }

  function setFilters(nextFilters: WidgetFilters) {
    const next = {
      ...widget,
      config: { ...widget.config, filters: nextFilters },
    } as Widget;
    onUpdate(next);
  }

  function setSingleMeasure(measure: Measure) {
    if (isMultiSeries(widget)) return;
    const next = {
      ...widget,
      config: {
        ...(widget.config as KPIConfig | BarConfig | PieConfig | SparklineConfig),
        measure,
      },
    } as Widget;
    onUpdate(next);
  }

  function setSeries(measures: SeriesConfig[]) {
    if (!isMultiSeries(widget)) return;
    const next: Widget = {
      ...widget,
      config: { ...widget.config, measures },
    } as Widget;
    onUpdate(next);
  }

  function setPrimaryDimension(dim: Dimension) {
    if (widget.type === "kpi") return; // KPI has no dimensions
    const cfg = widget.config as
      | BarConfig
      | LineConfig
      | AreaConfig
      | PieConfig
      | SparklineConfig
      | StackedBarConfig
      | TableConfig;
    const dims = [...(cfg.dimensions ?? [])];
    dims[0] = dim;
    const next: Widget = {
      ...widget,
      config: { ...cfg, dimensions: dims },
    } as Widget;
    onUpdate(next);
  }

  function setSecondaryDimension(dim: Dimension | "") {
    if (widget.type === "kpi" || isSingleAggLocked(widget)) return;
    const cfg = widget.config as
      | BarConfig
      | LineConfig
      | AreaConfig
      | StackedBarConfig
      | TableConfig;
    const dims = [...(cfg.dimensions ?? [])];
    if (dim === "") {
      dims.splice(1, 1);
    } else {
      dims[1] = dim;
    }
    const next: Widget = {
      ...widget,
      config: { ...cfg, dimensions: dims },
    } as Widget;
    onUpdate(next);
  }

  function setComparePrior(value: boolean) {
    if (widget.type !== "kpi") return;
    const next: Widget = {
      ...widget,
      config: {
        ...(widget.config as KPIConfig),
        compare_prior_period: value,
      },
    };
    onUpdate(next);
  }

  function setTopN(value: number) {
    if (widget.type !== "pie") return;
    const next: Widget = {
      ...widget,
      config: { ...(widget.config as PieConfig), top_n: value },
    };
    onUpdate(next);
  }

  function setStacked(value: boolean) {
    if (widget.type !== "area" && widget.type !== "stacked_bar") return;
    const next: Widget = {
      ...widget,
      config: {
        ...(widget.config as AreaConfig | StackedBarConfig),
        stacked: value,
      },
    } as Widget;
    onUpdate(next);
  }

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

function Section({
  label,
  help,
  children,
}: {
  label: string;
  /** Optional help-tooltip key rendered as an info icon next to the label. */
  help?: HelpTooltipKey;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider text-text-muted">
        <span>{label}</span>
        {help && <HelpTooltip k={help} />}
      </div>
      {children}
    </div>
  );
}

function OverridePill() {
  return (
    <span
      data-testid="override-pill"
      className="ml-2 inline-flex items-center rounded-full bg-accent/15 px-2 py-0.5 text-[10px] font-medium text-accent"
    >
      Overrides canvas
    </span>
  );
}

function SingleMeasureEditor({
  measure,
  onChange,
}: {
  measure: Measure;
  onChange: (m: Measure) => void;
}) {
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
          {FIELD_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </Section>
    </>
  );
}

function MeasuresEditor({
  widget,
  onChange,
}: {
  widget: Widget & { config: LineConfig | AreaConfig | StackedBarConfig | TableConfig };
  onChange: (m: SeriesConfig[]) => void;
}) {
  const measures = widget.config.measures;
  const cap = widget.type === "table" ? MAX_TABLE_COLUMNS : MAX_SERIES;

  function update(idx: number, next: SeriesConfig) {
    const copy = [...measures];
    copy[idx] = next;
    onChange(copy);
  }

  function add() {
    if (measures.length >= cap) return;
    onChange([
      ...measures,
      { measure: { agg: "sum", field: "amount" } },
    ]);
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
      {measures.map((s, idx) => (
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
              value={s.measure.agg}
              onChange={(e) =>
                update(idx, {
                  ...s,
                  measure: { ...s.measure, agg: e.target.value as Aggregation },
                })
              }
              aria-label={`Series ${idx + 1} aggregation`}
              className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
            >
              {AGG_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <HelpTooltip k={AGG_HELP_KEY[s.measure.agg]} />
            <select
              value={s.measure.field}
              onChange={(e) =>
                update(idx, {
                  ...s,
                  measure: {
                    ...s.measure,
                    field: e.target.value as MeasureField,
                  },
                })
              }
              aria-label={`Series ${idx + 1} field`}
              className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
            >
              {FIELD_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      ))}
      {measures.length < cap && (
        <button
          type="button"
          data-testid="measure-add"
          onClick={add}
          className="rounded-md border border-dashed border-border px-2 py-1 text-xs text-text-secondary transition hover:border-accent hover:text-accent"
        >
          + Add {widget.type === "table" ? "column" : "series"}
        </button>
      )}
    </div>
  );
}

function FilterEditor({
  filters,
  canvasFilters,
  onChange,
}: {
  filters: WidgetFilters;
  canvasFilters: CanvasFilters;
  onChange: (next: WidgetFilters) => void;
}) {
  return (
    <div className="flex flex-col gap-4 rounded-md border border-border bg-bg p-3">
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        Filters (this widget)
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Date range
          {isFieldOverridden("date_range", filters, canvasFilters) && (
            <OverridePill />
          )}
        </div>
        <DatePresetChips
          value={filters.date_range}
          ariaPrefix="Widget"
          onChange={(next) =>
            onChange({
              ...filters,
              date_range: next || undefined,
            })
          }
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Accounts
          {isFieldOverridden("account_ids", filters, canvasFilters) && (
            <OverridePill />
          )}
        </div>
        <AccountFilter
          value={filters.account_ids ?? []}
          ariaPrefix="Widget account"
          label=""
          onChange={(account_ids) =>
            onChange({
              ...filters,
              account_ids: account_ids.length > 0 ? account_ids : undefined,
            })
          }
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Categories
          {isFieldOverridden("category_ids", filters, canvasFilters) && (
            <OverridePill />
          )}
        </div>
        <CategoryPicker
          value={filters.category_ids ?? []}
          label=""
          onChange={(category_ids) =>
            onChange({
              ...filters,
              category_ids: category_ids.length > 0 ? category_ids : undefined,
            })
          }
        />
      </div>

      <div className="flex flex-col gap-1">
        <TxnTypeRadioRow
          value={filters.txn_type}
          onChange={(txn_type) => onChange({ ...filters, txn_type })}
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="text-xs text-text-secondary">Amount range</div>
        <div className="flex gap-2">
          <input
            type="number"
            aria-label="Widget amount min"
            placeholder="min"
            value={filters.amount_range?.min ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                amount_range: {
                  ...(filters.amount_range ?? {}),
                  min: e.target.value === "" ? undefined : Number(e.target.value),
                },
              })
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
          <input
            type="number"
            aria-label="Widget amount max"
            placeholder="max"
            value={filters.amount_range?.max ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                amount_range: {
                  ...(filters.amount_range ?? {}),
                  max: e.target.value === "" ? undefined : Number(e.target.value),
                },
              })
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
        </div>
      </div>

      <TagFilter
        value={filters.tag_names ?? []}
        match={(filters.tag_match ?? "all") as TagMatch}
        onChange={({ tag_names, tag_match }) =>
          onChange({
            ...filters,
            tag_names: tag_names.length > 0 ? tag_names : undefined,
            tag_match: tag_names.length > 0 ? tag_match : undefined,
          })
        }
      />
    </div>
  );
}

function TxnTypeRadioRow({
  value,
  onChange,
}: {
  value: "income" | "expense" | "transfer" | undefined;
  onChange: (next: "income" | "expense" | "transfer" | undefined) => void;
}) {
  const name = useId();
  const choices: Array<{ value: "" | "income" | "expense" | "transfer"; label: string }> = [
    { value: "", label: "Any" },
    { value: "income", label: "Income" },
    { value: "expense", label: "Expense" },
    { value: "transfer", label: "Transfer" },
  ];
  return (
    <>
      <div className="text-xs text-text-secondary">Transaction type</div>
      <div className="flex flex-wrap gap-3 text-xs text-text-secondary">
        {choices.map((c) => (
          <label key={c.value} className="flex items-center gap-1">
            <input
              type="radio"
              name={name}
              aria-label={`Widget transaction type ${c.label}`}
              checked={(value ?? "") === c.value}
              onChange={() => onChange(c.value === "" ? undefined : (c.value as "income" | "expense" | "transfer"))}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </>
  );
}
