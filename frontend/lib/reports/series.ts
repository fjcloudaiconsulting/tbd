/**
 * Helpers for multi-series widgets (Line / Area / StackedBar / Table).
 *
 * Backend AST carries one ``measure`` per request, so a widget with N
 * series fires N parallel queries and stitches the rows here by the
 * shared dimension key.
 */
import type {
  Measure,
  QueryRow,
  ReportsQueryResponse,
  SeriesConfig,
} from "./types";

const HUMAN_AGG: Record<Measure["agg"], string> = {
  sum: "Sum",
  count: "Count",
  avg: "Average",
  distinct: "Distinct",
};

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
  if (total === 1) return s.measure.field;
  return `${HUMAN_AGG[s.measure.agg]} of ${s.measure.field}`;
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
