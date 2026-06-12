"use client";

/**
 * Table widget — sortable, paginated rows. Columns are
 * ``dimensions`` (categorical) followed by one column per entry in
 * ``config.measures`` (numeric). Click a header to sort ascending /
 * descending; click again to flip. Pagination kicks in when more than
 * one page of rows exists (default page size: 25, matching the shared
 * system default; user-selectable via the per-page dropdown).
 *
 * **Spec ambiguity resolved (PR3, 2026-05-22):** each ``measures``
 * entry is its own column with its OWN aggregation. A report can mix
 * "Sum of amount" and "Count of id" side by side. Documented in the
 * task report.
 *
 * Currency formatting follows ``config.format`` for every numeric
 * column; if a single column needs a different format, future PRs
 * can extend ``SeriesConfig`` with a per-column ``format`` override.
 */
import { useMemo, useState } from "react";

import { useSeriesQueries } from "@/lib/reports/useReportQuery";
import {
  DIMENSION_HEADERS,
  formatMeasureValue,
  mergeSeriesRowsForTable,
  seriesLabel,
} from "@/lib/reports/series";
import type {
  CanvasFilters,
  TableWidget as TableWidgetType,
} from "@/lib/reports/types";
import Pagination from "@/components/ui/Pagination";
import { pageCount } from "@/lib/hooks/use-table-state";
import WidgetCsvButton from "./WidgetCsvButton";
import type { CsvCell } from "@/lib/reports/csv";

interface Props {
  widget: TableWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
}

const DEFAULT_PAGE_SIZE = 25;

export default function TableWidget({ widget, canvasFilters, editMode }: Props) {
  const measures = widget.config.measures.map((m) => m.measure);
  const { series, isLoading, error } = useSeriesQueries(
    widget,
    canvasFilters,
    measures,
  );

  const measuresConfig = widget.config.measures;
  // Memoize the derived series keys/labels and the merged table rows on
  // their real inputs so unrelated parent renders (sort toggle, page
  // change, hover) don't rebuild these arrays and re-run the O(n) merge.
  const seriesKeys = useMemo(
    () => measuresConfig.map((_, i) => `s${i}`),
    [measuresConfig],
  );
  const seriesLabels = useMemo(
    () => measuresConfig.map((m, i) => seriesLabel(m, i, measuresConfig.length)),
    [measuresConfig],
  );
  const dimensions = widget.config.dimensions;
  const rows = useMemo(
    () => mergeSeriesRowsForTable(series, dimensions, seriesKeys),
    [series, dimensions, seriesKeys],
  );

  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  const sortedRows = useMemo(() => {
    if (!sortKey) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (typeof av === "number" && typeof bv === "number") {
        return sortDir === "asc" ? av - bv : bv - av;
      }
      const as = String(av ?? "");
      const bs = String(bv ?? "");
      return sortDir === "asc" ? as.localeCompare(bs) : bs.localeCompare(as);
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  const totalPages = pageCount(sortedRows.length, pageSize);
  const safePage = Math.min(page, totalPages - 1);
  const pagedRows = sortedRows.slice(
    safePage * pageSize,
    safePage * pageSize + pageSize,
  );

  function toggleSort(key: string) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
    setPage(0);
  }

  const format = widget.config.format ?? "number";

  // Total row: sum each measure column across the FULL result set
  // (every row the widget holds in memory), not just the visible page.
  // Only additive aggregations (sum/count) get a real total; for
  // avg/distinct/min/max a column sum is meaningless, so we render a
  // placeholder rather than fabricate a wrong number.
  //
  // Caveat: this totals the rows the query returned, which are subject
  // to the widget's ``limit``. It is NOT a separate server-side grand
  // total. Fine for v1.
  const columnTotals = useMemo(() => {
    return widget.config.measures.map((m, i) => {
      const agg = m.measure.agg;
      const additive = agg === "sum" || agg === "count";
      if (!additive) return null;
      const key = seriesKeys[i];
      let sum = 0;
      for (const row of rows) {
        const v = row[key];
        const n = typeof v === "number" ? v : Number(v);
        if (Number.isFinite(n)) sum += n;
      }
      return sum;
    });
  }, [widget.config.measures, rows, seriesKeys]);

  // CSV export mirrors the rendered table: dimension columns followed by
  // one column per measure, every row the widget holds (in the current
  // sort order), then a Total row matching the footer.
  const csvDataset = useMemo(() => {
    const headers = [
      ...dimensions.map((d) => DIMENSION_HEADERS[d] ?? d),
      ...seriesLabels,
    ];
    const dataRows: CsvCell[][] = sortedRows.map((row) => [
      ...dimensions.map((d) => String(row[d] ?? "—")),
      ...seriesKeys.map((key) =>
        typeof row[key] === "number" ? (row[key] as number) : null,
      ),
    ]);
    if (dataRows.length > 0) {
      const totalRow: CsvCell[] = [
        ...dimensions.map((_, di) => (di === 0 ? "Total" : "")),
        ...columnTotals.map((t) => (t === null ? "" : t)),
      ];
      dataRows.push(totalRow);
    }
    return { headers, rows: dataRows };
  }, [dimensions, seriesLabels, sortedRows, seriesKeys, columnTotals]);

  return (
    <div
      data-testid="table-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div
          className="text-sm font-semibold text-text-primary"
          aria-label={widget.title || "Table"}
        >
          {widget.title || "Table"}
        </div>
        <WidgetCsvButton
          title={widget.title || "Table"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div
            data-testid="table-widget-loading"
            className="h-12 w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid="table-widget-error"
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : rows.length === 0 ? (
          <div
            data-testid="table-widget-empty"
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border text-[11px] uppercase tracking-wider text-text-muted">
                {dimensions.map((d) => (
                  <th key={d} className="py-2 pr-3">
                    <button
                      type="button"
                      data-testid={`table-widget-sort-${d}`}
                      onClick={() => toggleSort(d)}
                      className="font-semibold hover:text-text-primary"
                    >
                      {DIMENSION_HEADERS[d] ?? d}
                      {sortKey === d ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                    </button>
                  </th>
                ))}
                {seriesKeys.map((key, i) => (
                  <th key={key} className="py-2 pr-3 text-right">
                    <button
                      type="button"
                      data-testid={`table-widget-sort-${key}`}
                      onClick={() => toggleSort(key)}
                      className="font-semibold hover:text-text-primary"
                    >
                      {seriesLabels[i]}
                      {sortKey === key ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pagedRows.map((row, ridx) => (
                <tr
                  key={`row-${safePage}-${ridx}`}
                  className="border-b border-border-subtle text-text-primary"
                >
                  {dimensions.map((d) => (
                    <td key={d} className="py-1.5 pr-3">
                      {String(row[d] ?? "—")}
                    </td>
                  ))}
                  {seriesKeys.map((key) => (
                    <td key={key} className="py-1.5 pr-3 text-right font-mono">
                      {formatCell(row[key], format)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr
                data-testid="table-widget-total-row"
                className="border-t-2 border-border font-semibold text-text-primary"
              >
                {dimensions.map((d, di) => (
                  <td key={d} className="py-1.5 pr-3">
                    {di === 0 ? "Total" : ""}
                  </td>
                ))}
                {seriesKeys.map((key, i) => (
                  <td key={key} className="py-1.5 pr-3 text-right font-mono">
                    {columnTotals[i] === null
                      ? "—"
                      : formatCell(columnTotals[i], format)}
                  </td>
                ))}
              </tr>
            </tfoot>
          </table>
        )}
      </div>
      {totalPages > 1 && (
        <div data-testid="table-widget-pagination" className="mt-2">
          <Pagination
            page={safePage + 1}
            pageSize={pageSize}
            total={sortedRows.length}
            onPageChange={(n) => setPage(n - 1)}
            onPageSizeChange={(n) => {
              setPageSize(n);
              setPage(0);
            }}
          />
        </div>
      )}
    </div>
  );
}

function formatCell(
  v: string | number | null | undefined,
  format: "currency" | "number" | "percent",
): string {
  if (v === null || v === undefined) return "";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  return formatMeasureValue(n, format);
}
