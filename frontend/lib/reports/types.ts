/**
 * Reports v2 — frontend types.
 *
 * Mirrors the backend Pydantic shapes from
 * ``backend/app/schemas/reports_query.py`` and
 * ``backend/app/schemas/report.py``. Kept narrow + closed so the
 * widget components and config rail can rely on enum members being
 * the only valid values. Anything outside these unions is a wire
 * contract violation and the server will 422 it.
 */

export type WidgetType =
  | "kpi"
  | "bar"
  | "stacked_bar"
  | "line"
  | "area"
  | "pie"
  | "sparkline"
  | "table";

// PR3 exposes the full v1 catalog. ``WidgetTypeV1`` retains its name
// as the "widgets shipped in v1" union; the picker hands one of these
// values to the editor's ``addWidget`` factory.
export type WidgetTypeV1 = WidgetType;

export type Dataset = "transactions" | "accounts" | "recurring";

export type Aggregation = "sum" | "count" | "avg" | "distinct";

export type MeasureField =
  | "amount"
  | "id"
  | "category_id"
  | "account_id"
  | "balance";

export type Dimension =
  | "category"
  | "category_master"
  | "account"
  | "tag"
  | "txn_type"
  | "status"
  | "month"
  | "week"
  | "day"
  | "account_type"
  | "currency"
  | "account_active"
  | "frequency"
  | "recurring_active";

export type FilterField =
  | "date"
  | "amount"
  | "category_id"
  | "account_id"
  | "txn_type"
  | "status"
  | "tag_name";

export type FilterOp = "eq" | "in" | "between" | "gte" | "lte";

export type TagMatch = "all" | "any";

export type FilterValue =
  | string
  | number
  | boolean
  | Array<string | number>;

export interface Measure {
  agg: Aggregation;
  field: MeasureField;
}

export interface Filter {
  field: FilterField;
  op: FilterOp;
  value: FilterValue;
  // Tag-only knob; default ``all``. Ignored on non-tag filters.
  tag_match?: TagMatch;
}

export interface SortSpec {
  by: "value" | "dimension";
  dir: "asc" | "desc";
}

export interface ReportsQuery {
  dataset: Dataset;
  measure: Measure;
  dimensions: Dimension[];
  filters: Filter[];
  sort?: SortSpec;
  limit?: number;
}

export interface QueryMeta {
  row_count: number;
  truncated: boolean;
  query_ms: number;
}

export type QueryRow = Record<string, string | number | null>;

export interface ReportsQueryResponse {
  rows: QueryRow[];
  meta: QueryMeta;
}

// ─── canvas filters (cascade source) ────────────────────────────

export interface CanvasDateRange {
  /** ISO date YYYY-MM-DD */
  start?: string;
  /** ISO date YYYY-MM-DD */
  end?: string;
}

export interface CanvasFilters {
  // Phase 4b: the canvas filter bar shrank to a shared DATE control
  // only. Accounts and categories are now edited per-widget (in the
  // widget popover's Filters tab), so they no longer live here.
  // Saved ``canvas_filters_json`` blobs that still carry the old
  // ``account_ids`` / ``category_ids`` keys are tolerated by the
  // ``as CanvasFilters`` hydrate cast and simply never read.
  date_range?: CanvasDateRange;
}

// ─── widget layout / config ─────────────────────────────────────

export interface WidgetGrid {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface WidgetFilters {
  // Per-widget filter overrides. Any field present here overrides
  // the canvas-wide value for the same field. Empty / undefined
  // means "inherit from canvas filters."
  date_range?: CanvasDateRange;
  account_ids?: number[];
  category_ids?: number[];
  txn_type?: "income" | "expense" | "transfer";
  amount_range?: { min?: number; max?: number };
  tag_names?: string[];
  tag_match?: TagMatch;
}

export interface KPIConfig {
  dataset: Dataset;
  measure: Measure;
  filters?: WidgetFilters;
  format?: "currency" | "number" | "percent";
  /**
   * When true, the KPI widget renders a delta versus the immediately-
   * prior period of the same length as the resolved date range. v1
   * only honors this when the resolved date range is bounded; an
   * unbounded range hides the delta.
   */
  compare_prior_period?: boolean;
}

export interface BarConfig {
  dataset: Dataset;
  measure: Measure;
  dimensions: Dimension[];
  filters?: WidgetFilters;
  sort?: SortSpec;
  limit?: number;
  format?: "currency" | "number" | "percent";
}

export interface BaseWidget<T extends WidgetType, C> {
  id: string;
  type: T;
  title: string;
  grid: WidgetGrid;
  config: C;
}

export interface SeriesConfig {
  measure: Measure;
  /** Optional label override; defaults to "<agg> of <field>" when blank. */
  label?: string;
}

/**
 * Multi-series widget config. The backend AST carries one ``measure``
 * per request, so widgets that render multiple series (line, area,
 * stacked bar) fire one ``runQuery`` per entry in ``measures`` and the
 * client merges the rows by the dimension key. ``measures`` always
 * has at least one entry; UIs that pin to a single series (pie,
 * sparkline) keep this list at length 1.
 */
export interface SeriesWidgetConfig {
  dataset: Dataset;
  measures: SeriesConfig[];
  dimensions: Dimension[];
  filters?: WidgetFilters;
  sort?: SortSpec;
  limit?: number;
  format?: "currency" | "number" | "percent";
}

export interface LineConfig extends SeriesWidgetConfig {
  /** Visual register only; no AST impact. */
  smooth?: boolean;
}

export interface AreaConfig extends SeriesWidgetConfig {
  /** When multiple series are configured, stack them. */
  stacked?: boolean;
}

export interface PieConfig {
  dataset: Dataset;
  measure: Measure;
  dimensions: Dimension[]; // pinned to length 1 by the UI
  filters?: WidgetFilters;
  sort?: SortSpec;
  limit?: number;
  format?: "currency" | "number" | "percent";
  /** Slices beyond ``top_n`` are folded into an "Other" bucket. */
  top_n?: number;
}

export interface SparklineConfig {
  dataset: Dataset;
  measure: Measure;
  dimensions: Dimension[]; // exactly one time-bucket dimension
  filters?: WidgetFilters;
  sort?: SortSpec;
  limit?: number;
  format?: "currency" | "number" | "percent";
}

export interface StackedBarConfig extends SeriesWidgetConfig {
  /** Defaults to true — a stacked bar with stacking off is just a bar
   * chart, so this only flips off when the user explicitly wants
   * grouped (side-by-side) bars from the same widget. */
  stacked?: boolean;
}

export interface TableConfig {
  dataset: Dataset;
  /** 1..5 entries. Each becomes a numeric column on the table. */
  measures: SeriesConfig[];
  dimensions: Dimension[];
  filters?: WidgetFilters;
  sort?: SortSpec;
  limit?: number;
  format?: "currency" | "number" | "percent";
}

export type KPIWidget = BaseWidget<"kpi", KPIConfig>;
export type BarWidget = BaseWidget<"bar", BarConfig>;
export type LineWidget = BaseWidget<"line", LineConfig>;
export type AreaWidget = BaseWidget<"area", AreaConfig>;
export type PieWidget = BaseWidget<"pie", PieConfig>;
export type SparklineWidget = BaseWidget<"sparkline", SparklineConfig>;
export type StackedBarWidget = BaseWidget<"stacked_bar", StackedBarConfig>;
export type TableWidget = BaseWidget<"table", TableConfig>;

// Full v1 widget union. PR3 expands this from KPI + Bar to the
// architect-locked eight-widget catalog (spec §2 "Widget catalog v1").
export type Widget =
  | KPIWidget
  | BarWidget
  | LineWidget
  | AreaWidget
  | PieWidget
  | SparklineWidget
  | StackedBarWidget
  | TableWidget;

export interface LayoutJson {
  version: 1;
  widgets: Widget[];
}

// ─── REST API shapes ────────────────────────────────────────────

export type ReportVisibility = "private" | "org";

export interface ReportSummary {
  id: number;
  owner_user_id: number;
  org_id: number;
  visibility: ReportVisibility;
  name: string;
  description: string | null;
  layout_json: LayoutJson | Record<string, never>;
  canvas_filters_json: CanvasFilters | Record<string, never>;
  schema_version: number;
  created_at: string;
  updated_at: string;
}

export interface ReportVersionSummary {
  id: number;
  is_original: boolean;
  created_at: string;
}

export interface ReportTemplate {
  key: string;
  name: string;
  description: string;
  layout_json: LayoutJson;
  canvas_filters_json: CanvasFilters;
}

export interface ReportCreatePayload {
  name: string;
  description?: string;
  visibility?: ReportVisibility;
  layout_json?: LayoutJson | Record<string, never>;
  canvas_filters_json?: CanvasFilters | Record<string, never>;
}

export interface ReportUpdatePayload {
  name?: string;
  description?: string | null;
  visibility?: ReportVisibility;
  layout_json?: LayoutJson | Record<string, never>;
  canvas_filters_json?: CanvasFilters | Record<string, never>;
}

// ─── source catalog (GET /api/v1/reports/sources) ───────────────
//
// The self-describing data-source registry. Each entry declares the
// dimensions, measures, and filters a source supports; the widget
// editor drives its pickers off the selected source's catalog so a
// widget can never offer (and then 422 on) an out-of-source field.

export interface SourceCatalogFilter {
  field: string;
  label: string;
  ops: string[];
  kind: string;
}

export interface SourceCatalogDimension {
  key: string;
  label: string;
  kind: string;
}

export interface SourceCatalogMeasure {
  key: string;
  label: string;
  agg: string;
  field: string;
  format: string;
}

export interface SourceCatalogEntry {
  key: string;
  label: string;
  dimensions: SourceCatalogDimension[];
  measures: SourceCatalogMeasure[];
  filters: SourceCatalogFilter[];
}
