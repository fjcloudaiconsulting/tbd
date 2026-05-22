"use client";

/**
 * Right-rail widget configuration UI. Slides in when a widget is
 * selected in edit mode. Mutations call back into the editor's
 * ``onUpdate`` so the report's full layout state stays the single
 * source of truth (debounced save handled at the editor level).
 *
 * For each field with a per-widget override, the rail surfaces an
 * "Overrides canvas" pill (spec §4) computed via ``isFieldOverridden``.
 */
import type {
  BarConfig,
  CanvasFilters,
  Dimension,
  KPIConfig,
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

const AGG_OPTIONS: Array<{ value: "sum" | "count" | "avg" | "distinct"; label: string }> = [
  { value: "sum", label: "Sum" },
  { value: "count", label: "Count" },
  { value: "avg", label: "Average" },
  { value: "distinct", label: "Distinct count" },
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

  function setAgg(agg: "sum" | "count" | "avg" | "distinct") {
    // Keep widget.config narrow per type — Bar and KPI share the
    // measure shape.
    const next = {
      ...widget,
      config: {
        ...widget.config,
        measure: { ...widget.config.measure, agg },
      },
    } as Widget;
    onUpdate(next);
  }

  function setFilters(nextFilters: WidgetFilters) {
    const next = {
      ...widget,
      config: { ...widget.config, filters: nextFilters },
    } as Widget;
    onUpdate(next);
  }

  function setDimension(dim: Dimension) {
    if (widget.type !== "bar") return;
    const next: Widget = {
      ...widget,
      config: { ...(widget.config as BarConfig), dimensions: [dim] },
    };
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

  return (
    <aside
      data-testid="config-rail"
      className="flex h-full w-80 shrink-0 flex-col gap-4 border-l border-border bg-surface p-4 overflow-y-auto"
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

      <Section label="Aggregation">
        <select
          value={widget.config.measure.agg}
          onChange={(e) =>
            setAgg(e.target.value as "sum" | "count" | "avg" | "distinct")
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

      {widget.type === "bar" && (
        <Section label="Dimension">
          <select
            value={widget.config.dimensions[0] ?? "category"}
            onChange={(e) => setDimension(e.target.value as Dimension)}
            aria-label="Dimension"
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

      {widget.type === "kpi" && (
        <Section label="Compare to prior period">
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input
              type="checkbox"
              checked={Boolean(widget.config.compare_prior_period)}
              onChange={(e) => setComparePrior(e.target.checked)}
              aria-label="Compare to prior period"
            />
            <span>Show delta vs prior period</span>
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
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        {label}
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
        <div className="flex gap-2">
          <input
            type="date"
            aria-label="Widget date from"
            value={filters.date_range?.start ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                date_range: {
                  ...(filters.date_range ?? {}),
                  start: e.target.value || undefined,
                },
              })
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
          <input
            type="date"
            aria-label="Widget date to"
            value={filters.date_range?.end ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                date_range: {
                  ...(filters.date_range ?? {}),
                  end: e.target.value || undefined,
                },
              })
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Accounts (ids)
          {isFieldOverridden("account_ids", filters, canvasFilters) && (
            <OverridePill />
          )}
        </div>
        <input
          type="text"
          aria-label="Widget accounts"
          inputMode="numeric"
          placeholder="e.g. 12, 14"
          value={(filters.account_ids ?? []).join(",")}
          onChange={(e) =>
            onChange({
              ...filters,
              account_ids: parseIdList(e.target.value),
            })
          }
          className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Categories (ids)
          {isFieldOverridden("category_ids", filters, canvasFilters) && (
            <OverridePill />
          )}
        </div>
        <input
          type="text"
          aria-label="Widget categories"
          inputMode="numeric"
          placeholder="e.g. 3, 5"
          value={(filters.category_ids ?? []).join(",")}
          onChange={(e) =>
            onChange({
              ...filters,
              category_ids: parseIdList(e.target.value),
            })
          }
          className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="text-xs text-text-secondary">Transaction type</div>
        <select
          value={filters.txn_type ?? ""}
          aria-label="Widget transaction type"
          onChange={(e) =>
            onChange({
              ...filters,
              txn_type:
                e.target.value === ""
                  ? undefined
                  : (e.target.value as "income" | "expense" | "transfer"),
            })
          }
          className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        >
          <option value="">Any</option>
          <option value="income">Income</option>
          <option value="expense">Expense</option>
          <option value="transfer">Transfer</option>
        </select>
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

      <div className="flex flex-col gap-1">
        <div className="text-xs text-text-secondary">Tags</div>
        <input
          type="text"
          aria-label="Widget tags"
          placeholder="comma list of tag names"
          value={(filters.tag_names ?? []).join(",")}
          onChange={(e) =>
            onChange({
              ...filters,
              tag_names: e.target.value
                .split(",")
                .map((s) => s.trim().toLowerCase())
                .filter(Boolean),
            })
          }
          className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
        <div className="mt-1 flex gap-3 text-xs text-text-secondary">
          <label className="flex items-center gap-1">
            <input
              type="radio"
              name="tag-match"
              aria-label="Tag match all"
              checked={(filters.tag_match ?? "all") === "all"}
              onChange={() => onChange({ ...filters, tag_match: "all" })}
            />
            <span>Match all</span>
          </label>
          <label className="flex items-center gap-1">
            <input
              type="radio"
              name="tag-match"
              aria-label="Tag match any"
              checked={filters.tag_match === "any"}
              onChange={() =>
                onChange({ ...filters, tag_match: "any" as TagMatch })
              }
            />
            <span>Match any</span>
          </label>
        </div>
      </div>
    </div>
  );
}

function parseIdList(raw: string): number[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => Number(s))
    .filter((n) => Number.isInteger(n) && n > 0);
}
