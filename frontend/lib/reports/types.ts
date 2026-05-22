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

// v1 only ships kpi + bar; the rest are declared so the layout JSON
// schema is forward-compatible with PR3 widgets without a migration.
export type WidgetTypeV1 = "kpi" | "bar";

export type Dataset = "transactions";

export type Aggregation = "sum" | "count" | "avg" | "distinct";

export type MeasureField = "amount" | "id" | "category_id" | "account_id";

export type Dimension =
  | "category"
  | "category_master"
  | "account"
  | "tag"
  | "txn_type"
  | "status"
  | "month"
  | "week"
  | "day";

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
  date_range?: CanvasDateRange;
  account_ids?: number[];
  category_ids?: number[];
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

export type KPIWidget = BaseWidget<"kpi", KPIConfig>;
export type BarWidget = BaseWidget<"bar", BarConfig>;

// v1 widget union. PR3 adds the rest.
export type Widget = KPIWidget | BarWidget;

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
