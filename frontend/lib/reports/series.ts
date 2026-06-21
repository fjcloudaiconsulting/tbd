/**
 * Helpers for multi-series widgets (Line / Area / StackedBar / Table).
 *
 * Backend AST carries one ``measure`` per request, so a widget with N
 * series fires N parallel queries and stitches the rows here by the
 * shared dimension key.
 */
import { formatAmount } from "@/lib/format";
import type {
  Dimension,
  Measure,
  MeasureField,
  QueryRow,
  ReportsQueryResponse,
  SeriesConfig,
} from "./types";

// Matches AGG_OPTIONS labels in controlConstants so the editor picker and the
// rendered series/tooltip/CSV labels agree (distinct was "Distinct count" in
// the picker but "Distinct" here).
const HUMAN_AGG: Record<Measure["agg"], string> = {
  sum: "Sum",
  count: "Count",
  avg: "Average",
  distinct: "Distinct count",
};

/**
 * Human-readable column header per dimension. Shared by the table
 * widget's header row and the CSV export of every dimension-bearing
 * widget so the exported header matches what the user sees.
 */
export const DIMENSION_HEADERS: Record<Dimension, string> = {
  category: "Category",
  category_master: "Master category",
  account: "Account",
  tag: "Tag",
  txn_type: "Transaction type",
  status: "Status",
  month: "Month",
  week: "Week",
  day: "Day",
  account_type: "Account type",
  currency: "Currency",
  account_active: "Active",
  frequency: "Frequency",
  recurring_active: "Status",
};

/** Header label for a dimension key, falling back to the raw key. */
export function dimensionHeader(key: string): string {
  return DIMENSION_HEADERS[key as Dimension] ?? key;
}

/**
 * Friendly display label for a measure field. ``measure.field`` is a raw
 * DB key ("amount", "id", "category_id", "account_id"); surfaced bare it
 * reads "amount: 1234" or "account_id: 3" in chart tooltips and CSV
 * headers. Single source of truth for the human label — consumed by
 * ``seriesLabel`` (multi-series chart names), the single-series bar
 * tooltip + CSV header (``BarWidget``), and the widget-editor field picker.
 */
export const MEASURE_FIELD_LABELS: Record<MeasureField, string> = {
  amount: "Amount",
  id: "Row count",
  category_id: "Category",
  account_id: "Account",
  balance: "Balance",
};

/** Display label for a measure field, falling back to the raw key. */
export function measureFieldLabel(field: MeasureField): string {
  return MEASURE_FIELD_LABELS[field] ?? field;
}

/**
 * Best-effort ISO-code → currency-symbol map, mirroring the dashboard's
 * ``AccountMonthEndForecast`` helper. Unknown codes fall back to the code
 * itself with a trailing space (e.g. "CHF 1,234.56") so the value stays
 * readable. A report is single-currency in practice — cross-currency
 * mixing is deliberately NOT done — so a report's charts share one symbol
 * derived from the org's accounts via ``reportCurrency``.
 */
const CURRENCY_SYMBOLS: Record<string, string> = {
  EUR: "€",
  USD: "$",
  GBP: "£",
};

/** Symbol (or padded ISO code) prefix for a currency code. */
export function currencySymbol(code: string | undefined | null): string {
  if (!code) return "";
  return CURRENCY_SYMBOLS[code] ?? `${code} `;
}

/**
 * Derive the single currency a report renders in from the org's accounts.
 * Reports are single-currency in practice (cross-currency mixing is not
 * done), so we take the first account's currency as the report currency.
 * Returns ``undefined`` when no account currency is available, in which
 * case currency formatting degrades to grouped numbers with no symbol.
 *
 * Trade-off knowingly accepted: if an org legitimately holds accounts in
 * more than one currency, every widget is labeled with the first account's
 * symbol — a measure aggregating a differently-denominated account would be
 * mislabeled. This matches the rest of the report engine, which is not
 * currency-aware. A future gate (show no symbol when >1 distinct currency)
 * is the cleaner fix; tracked in the reports backlog.
 */
export function reportCurrency(
  accounts: Array<{ currency?: string | null }> | undefined | null,
): string | undefined {
  const code = accounts?.find((a) => a.currency)?.currency;
  return code ?? undefined;
}

/** Format a measure value for display in widget tooltips, axes and cells.
 *  Grouped numbers; when ``format`` is "currency" and a ``currency`` ISO
 *  code is supplied, the org currency symbol is prefixed (e.g. "€1,234.56").
 *  With no currency code, currency formatting degrades to grouped 2-decimal
 *  numbers with no symbol, matching the app's `formatAmount` convention. */
export function formatMeasureValue(
  value: number,
  format: "currency" | "number" | "percent",
  currency?: string,
): string {
  // Recharts hands tick/tooltip values in as `any`; a degenerate
  // undefined/NaN would otherwise surface as the literal "NaN". Mirror
  // the Number.isFinite guard KPIWidget/TableWidget already apply.
  if (!Number.isFinite(value)) return "";
  if (format === "percent") return `${value.toFixed(1)}%`;
  if (format === "currency") {
    // grouped, 2dp; symbol prefix when the org currency is known.
    return `${currencySymbol(currency)}${formatAmount(value)}`;
  }
  return value.toLocaleString();
}

/**
 * Stable human label for a series. Uses the optional ``label`` if
 * present; otherwise falls back to "<Agg> of <field>". A single-
 * series widget shows just the field name to avoid the noisy
 * "Sum of amount" register for the common case.
 */
export function seriesLabel(
  s: SeriesConfig,
  index: number,
  total: number,
): string {
  if (s.label && s.label.trim()) return s.label.trim();
  const fieldLabel = measureFieldLabel(s.measure.field);
  if (total === 1) return fieldLabel;
  return `${HUMAN_AGG[s.measure.agg]} of ${fieldLabel}`;
}

/**
 * Merge per-series query responses into a single row list, keyed by
 * the dimension value. Each series' rows are aligned by their
 * dimension key and the measure value lands under ``seriesKeys[i]``.
 *
 * Missing dimension values across series are filled with 0 so
 * Recharts doesn't drop the data point.
 */
export function mergeSeriesRows(
  series: Array<ReportsQueryResponse | undefined>,
  dimensionKey: string,
  seriesKeys: string[],
): Array<{ label: string } & Record<string, number | string>> {
  const byLabel = new Map<string, Record<string, number | string>>();
  const order: string[] = [];
  series.forEach((resp, i) => {
    if (!resp) return;
    const key = seriesKeys[i];
    for (const row of resp.rows ?? []) {
      const label = readLabel(row, dimensionKey);
      let existing = byLabel.get(label);
      if (!existing) {
        existing = { label };
        byLabel.set(label, existing);
        order.push(label);
      }
      existing[key] = readNumber(row.value);
    }
  });
  // Backfill zeros so Recharts renders flat segments instead of gaps.
  for (const label of order) {
    const row = byLabel.get(label)!;
    for (const key of seriesKeys) {
      if (typeof row[key] !== "number") row[key] = 0;
    }
  }
  return order.map((l) => byLabel.get(l)!) as Array<
    { label: string } & Record<string, number | string>
  >;
}

/**
 * Merge per-series responses into a table-shaped row list. Used by
 * the table widget: each row carries the dimension columns plus one
 * numeric column per series.
 */
export function mergeSeriesRowsForTable(
  series: Array<ReportsQueryResponse | undefined>,
  dimensions: string[],
  seriesKeys: string[],
): Array<Record<string, number | string>> {
  const byKey = new Map<string, Record<string, number | string>>();
  const order: string[] = [];
  series.forEach((resp, i) => {
    if (!resp) return;
    const key = seriesKeys[i];
    for (const row of resp.rows ?? []) {
      const dimensionValues = dimensions.map((d) => readLabel(row, d));
      const rowKey = dimensionValues.join("");
      let existing = byKey.get(rowKey);
      if (!existing) {
        existing = {};
        dimensions.forEach((d, j) => {
          existing![d] = dimensionValues[j];
        });
        byKey.set(rowKey, existing);
        order.push(rowKey);
      }
      existing[key] = readNumber(row.value);
    }
  });
  for (const rowKey of order) {
    const row = byKey.get(rowKey)!;
    for (const key of seriesKeys) {
      if (typeof row[key] !== "number") row[key] = 0;
    }
  }
  return order.map((k) => byKey.get(k)!);
}

/**
 * Pivot a single two-dimension query result into stacked-bar rows.
 *
 * Given rows grouped by ``[primaryKey, secondaryKey]`` (e.g. category ×
 * account), produce one row per primary value whose numeric fields are
 * keyed by each distinct secondary value. Recharts then renders one
 * ``<Bar dataKey={secondaryValue} stackId>`` per secondary value, so a
 * single total bar is sliced into one colored segment per secondary
 * value (e.g. per account).
 *
 * Returns the pivoted rows, the ordered list of distinct secondary
 * values (first-seen order, for legend labels), and a matching list of
 * STABLE generated keys (``s0``, ``s1``, …). Numeric fields are keyed by
 * the generated key, NOT the raw secondary value: a raw value used as a
 * Recharts ``dataKey`` is fragile (a "." is parsed as a nested path, and
 * special chars / collisions can stop bars from rendering). Callers pair
 * ``seriesKeys[i]`` (the dataKey) with ``secondaryValues[i]`` (the
 * display label). Missing combinations are backfilled with 0 so Recharts
 * renders flat segments instead of gaps.
 */
export function pivotBySecondaryDimension(
  rows: QueryRow[],
  primaryKey: string,
  secondaryKey: string,
): {
  rows: Array<{ label: string } & Record<string, number | string>>;
  secondaryValues: string[];
  seriesKeys: string[];
} {
  const byLabel = new Map<string, Record<string, number | string>>();
  const order: string[] = [];
  const secondaryValues: string[] = [];
  // Map each distinct secondary value to a stable generated key. Using a
  // null-prototype Map key set + generated keys avoids prototype
  // pollution from user-controlled values like ``__proto__``.
  const keyForSecondary = new Map<string, string>();

  for (const row of rows) {
    const label = readLabel(row, primaryKey);
    const secondary = readLabel(row, secondaryKey);
    let seriesKey = keyForSecondary.get(secondary);
    if (seriesKey === undefined) {
      seriesKey = `s${secondaryValues.length}`;
      keyForSecondary.set(secondary, seriesKey);
      secondaryValues.push(secondary);
    }
    let existing = byLabel.get(label);
    if (!existing) {
      // Null-prototype record: user-controlled secondary values become
      // generated keys on a prototype-less object, so values like
      // ``__proto__`` are plain data and can't pollute Object.prototype.
      existing = Object.assign(
        Object.create(null) as Record<string, number | string>,
        { label },
      );
      byLabel.set(label, existing);
      order.push(label);
    }
    // Same (primary, secondary) pair shouldn't repeat after GROUP BY,
    // but sum defensively in case it does.
    const prior = typeof existing[seriesKey] === "number"
      ? (existing[seriesKey] as number)
      : 0;
    existing[seriesKey] = prior + readNumber(row.value);
  }

  const seriesKeys = secondaryValues.map((sv) => keyForSecondary.get(sv)!);

  for (const label of order) {
    const r = byLabel.get(label)!;
    for (const sk of seriesKeys) {
      if (typeof r[sk] !== "number") r[sk] = 0;
    }
  }

  return {
    rows: order.map((l) => byLabel.get(l)!) as Array<
      { label: string } & Record<string, number | string>
    >,
    secondaryValues,
    seriesKeys,
  };
}

function readLabel(
  row: QueryRow,
  key: string,
): string {
  const v = row[key];
  if (v === null || v === undefined) return "—";
  return String(v);
}

function readNumber(v: string | number | null | undefined): number {
  if (v === null || v === undefined) return 0;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Pie-specific top-N + "Other" bucket helper. Sorts by value desc,
 * keeps the top ``topN`` slices, and rolls the remainder into a
 * single "Other" slice when there are more.
 */
export function topNWithOther(
  rows: Array<{ label: string; value: number }>,
  topN: number,
): Array<{ label: string; value: number }> {
  if (rows.length <= topN) return rows;
  const sorted = [...rows].sort((a, b) => b.value - a.value);
  const head = sorted.slice(0, topN);
  const tail = sorted.slice(topN);
  const other = tail.reduce((sum, r) => sum + r.value, 0);
  return [...head, { label: "Other", value: other }];
}
