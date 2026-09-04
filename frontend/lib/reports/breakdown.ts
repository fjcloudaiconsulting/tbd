/**
 * Two-dimension break-down pipeline for the bar family (TBD-382).
 *
 * A bar broken down by a secondary dimension is ONE query grouped by
 * ``[primary, secondary]``, pivoted client-side. Three rulings live here, and
 * their ORDER is load-bearing:
 *
 *   1. pivot          — `pivotBySecondaryDimension` (lib/reports/series.ts)
 *   2. cap primaries  — R3
 *   3. fold secondaries into "Other" — R5
 *   4. assign colours from a stable label ordering — R4b
 *
 * ⚠ Why cap BEFORE fold. Ranking must only ever see rendered data. Fold-first
 * lets a secondary that appears solely under a primary the cap is about to
 * drop win a legend entry and a palette slot while contributing 0 to every
 * rendered bar, while a real, visible secondary is buried in "Other". Both
 * orders keep bar totals exact, so no arithmetic fence catches the difference
 * — it has to be specified. See F21.
 *
 * ⚠ Why this is not merged into `pivotBySecondaryDimension`. That function
 * answers "what are the distinct secondary values and their per-primary
 * values"; this one answers "which of them do we render, and in what colour".
 * `mergeSeriesRows` deliberately stays separate from both: it merges N
 * responses on one KNOWN key, and only the pivot needs the null-prototype
 * guard against `__proto__`.
 */
import { CHART_SERIES } from "@/lib/chart-colors";

import { pivotBySecondaryDimension } from "./series";
import type { Dimension, QueryRow, SortSpec } from "./types";

/**
 * The backend's own ceiling (`MAX_LIMIT` in
 * `backend/app/schemas/reports_query.py`). A two-dimension query asks for the
 * whole pair space and caps PRIMARIES client-side, because `limit` on the
 * wire caps ROWS and with two dimensions a row is a (primary, secondary)
 * PAIR — so `limit: 10` meant "at most ten pairs in total", and every
 * affected bar under-reported its own total (Defect C).
 */
export const MAX_QUERY_LIMIT = 500;

/**
 * Default primary cap when `config.limit` is absent on the 2-dimension path.
 *
 * `limit` never meant "primary buckets" before TBD-382 (it meant pairs), so
 * there is no prior primary-cap semantics to preserve. `emptyBar` writes
 * `limit: 10` explicitly, so factory-made bars are unaffected; this default
 * only reaches configs carrying no limit at all — which is exactly the
 * already-cloned-template case that must render all twelve of its months.
 */
export const DEFAULT_PRIMARY_CAP = 100;

/** The label of the folded remainder bucket. */
export const OTHER_LABEL = "Other";

/** The generated dataKey of the folded remainder bucket. */
export const OTHER_KEY = "sOther";

/**
 * "Other" renders in a NEUTRAL, never a categorical hue, and is pinned last
 * in both stack order and legend order so position is a second channel.
 * Falling through to `CHART_SERIES[7]` would paint "not a category" in
 * Overdue Coral, the danger hue — asserting a status that is not there.
 *
 * Measured 3.31:1 dark / 3.32:1 light against `bg-surface`. The
 * `PieWidgetChart` precedent uses `var(--color-border)`, which measures 1.35:1
 * and fails WCAG 1.4.11; this copies the SHAPE of that precedent, not its
 * token. Fixing Pie is filed separately.
 */
export const OTHER_COLOR = "var(--color-border-strong)";

/** Dimensions whose values are time buckets and read chronologically. */
const TIME_DIMENSIONS: ReadonlySet<string> = new Set(["month", "week", "day"]);

export function isTimeDimension(key: string | undefined): boolean {
  return key !== undefined && TIME_DIMENSIONS.has(key);
}

export type BreakdownRow = { label: string } & Record<string, number | string>;

export interface Breakdown {
  /** Rows handed to Recharts: capped primaries, folded secondary keys. */
  rows: BreakdownRow[];
  /** Legend / stack labels in render order ("Other" last when present). */
  secondaryValues: string[];
  /** Recharts dataKeys, parallel to ``secondaryValues``. */
  seriesKeys: string[];
  /** Fill colours, parallel to ``secondaryValues``. */
  sliceColors: string[];
  /**
   * RAW, UNFOLDED column labels for the CSV export, over the same capped
   * rows. docs/product/PRODUCT.md's line-item visibility principle requires every total
   * to have a path to its constituent rows, and the fold is a truncation the
   * user did not choose: 11 distinct secondary values means 12 CSV columns
   * (label + 11), not 9.
   */
  csvValues: string[];
  /** dataKeys parallel to ``csvValues``. */
  csvKeys: string[];
}

export const EMPTY_BREAKDOWN: Breakdown = {
  rows: [],
  secondaryValues: [],
  seriesKeys: [],
  sliceColors: [],
  csvValues: [],
  csvKeys: [],
};

function numeric(row: BreakdownRow, key: string): number {
  const v = row[key];
  return typeof v === "number" ? v : 0;
}

function rowTotal(row: BreakdownRow, keys: string[]): number {
  return keys.reduce((sum, k) => sum + numeric(row, k), 0);
}

/**
 * R3 — keep at most ``cap`` PRIMARY buckets, choosing by three branches
 * decided by the PRIMARY dimension first and ``sort`` second.
 *
 * ⚠ Branch 1 sorts BEFORE capping, and that is the whole point. "Keep backend
 * order and take the first N" is NOT chronological: with `sort` absent the
 * compiler applies `ORDER BY value DESC` over (primary, secondary) PAIRS, so
 * first-seen primary order is "months ranked by their single largest
 * category". Capping that drops an arbitrary subset of months out of the
 * middle of the series. Sorting after capping merely re-orders a set that
 * already lost the wrong members.
 */
export function capPrimaryBuckets(
  rows: BreakdownRow[],
  seriesKeys: string[],
  primaryKey: string,
  cap: number,
  sort: SortSpec | undefined,
): BreakdownRow[] {
  // 1. A time primary always reads chronologically, and keeps the TAIL —
  //    the most recent N. Precedent: networth.py:277-284.
  if (isTimeDimension(primaryKey)) {
    const chronological = [...rows].sort((a, b) =>
      String(a.label).localeCompare(String(b.label)),
    );
    return chronological.length > cap ? chronological.slice(-cap) : chronological;
  }

  // 2. An explicit dimension sort on a non-time primary is already the order
  //    the user asked for: keep it, take the head.
  if (sort?.by === "dimension") {
    return rows.length > cap ? rows.slice(0, cap) : rows;
  }

  // 3. Otherwise rank by the bucket's own TOTAL. `sort.dir` is honoured so a
  //    widget configured ascending is not silently re-ranked descending; the
  //    spec's ruling names desc, which is what an absent dir resolves to.
  const asc = sort?.dir === "asc";
  const ranked = [...rows].sort((a, b) => {
    const delta = rowTotal(a, seriesKeys) - rowTotal(b, seriesKeys);
    return asc ? delta : -delta;
  });
  return ranked.length > cap ? ranked.slice(0, cap) : ranked;
}

/**
 * Build the render-ready break-down for a two-dimension query result.
 *
 * ``limit`` is `config.limit` (a PRIMARY-bucket cap after TBD-382, not a row
 * cap); ``sort`` is `config.sort`.
 */
export function buildBreakdown(
  queryRows: QueryRow[],
  primaryKey: string,
  secondaryKey: string,
  options: { limit?: number; sort?: SortSpec } = {},
): Breakdown {
  const pivot = pivotBySecondaryDimension(queryRows, primaryKey, secondaryKey);
  const cap = options.limit ?? DEFAULT_PRIMARY_CAP;

  // ── 1. cap primaries ───────────────────────────────────────────────
  const rows = capPrimaryBuckets(
    pivot.rows,
    pivot.seriesKeys,
    primaryKey,
    cap,
    options.sort,
  );

  // ── 2. rank the secondaries that actually reach a rendered bar ─────
  // A secondary whose whole contribution sat under a dropped primary is not
  // in the chart, so it is not in the legend, the palette, or the export.
  const live = pivot.secondaryValues
    .map((label, i) => ({
      label,
      key: pivot.seriesKeys[i],
      total: rows.reduce((sum, r) => sum + numeric(r, pivot.seriesKeys[i]), 0),
    }))
    .filter((s) => s.total !== 0);

  // ── 3. the CSV keeps every live column, unfolded, alphabetically ───
  const csv = [...live].sort((a, b) => a.label.localeCompare(b.label));

  // ── 4. fold, only when the palette actually runs out ───────────────
  // ⚠ The trigger is `> CHART_SERIES.length` (i.e. > 8), NOT `>=`. The head
  // size is 7; a naive mirror of `topNWithOther` (`rows.length <= topN`) with
  // topN = 7 folds at exactly 8 and repaints a chart that renders perfectly
  // today with eight distinct colours.
  const folds = live.length > CHART_SERIES.length;
  const headSize = CHART_SERIES.length - 1;
  const byTotalDesc = [...live].sort((a, b) => b.total - a.total);
  const head = folds ? byTotalDesc.slice(0, headSize) : live;
  const tail = folds ? byTotalDesc.slice(headSize) : [];

  // ── 5. colour from a STABLE ordering of the label, not arrival order ─
  // `pivotBySecondaryDimension` mints s0..sN in FIRST-SEEN order and the
  // compiler defaults to ORDER BY value DESC, so arrival order is a function
  // of the values: Groceries would be gold this month and violet next. A
  // category changing its own colour between two loads is the same false
  // assertion of identity the fold exists to remove, on the time axis.
  const rendered = [...head].sort((a, b) => a.label.localeCompare(b.label));

  const secondaryValues = rendered.map((s) => s.label);
  const seriesKeys = rendered.map((s) => s.key);
  const sliceColors: string[] = rendered.map((_, i) => CHART_SERIES[i]);

  let outRows = rows;
  if (folds) {
    // The tail is SUMMED, never dropped: Sum(head) + Sum(tail) = Sum(all), so
    // the bar total stays exact. Dropping it would under-report the very
    // total this ticket exists to correct.
    outRows = rows.map((r) => ({
      ...r,
      [OTHER_KEY]: tail.reduce((sum, s) => sum + numeric(r, s.key), 0),
    }));
    secondaryValues.push(OTHER_LABEL);
    seriesKeys.push(OTHER_KEY);
    sliceColors.push(OTHER_COLOR);
  }

  return {
    rows: outRows,
    secondaryValues,
    seriesKeys,
    sliceColors,
    csvValues: csv.map((s) => s.label),
    csvKeys: csv.map((s) => s.key),
  };
}

/**
 * The AST limit for a bar-family widget.
 *
 * With two dimensions the wire limit caps PAIRS, so it goes to the ceiling
 * and `config.limit` becomes the client-side primary cap. With one dimension
 * `limit` is a genuine bucket cap and is left exactly as it was — note the
 * per-type default differs (`bar` 10, `stacked_bar` 100), which is why this
 * takes `singleDimensionDefault` rather than hardcoding one.
 */
export function astLimitForBarFamily(
  dimensions: Dimension[],
  configLimit: number | undefined,
  singleDimensionDefault: number,
): number {
  if (dimensions.length > 1) return MAX_QUERY_LIMIT;
  return configLimit ?? singleDimensionDefault;
}
