"use client";

/**
 * Table widget — sortable, paginated rows. Columns are
 * ``dimensions`` (categorical) followed by one column per entry in
 * ``config.measures`` (numeric). Click a header to sort ascending /
 * descending; click again to flip. Pagination kicks in past 50 rows
 * with a fixed 50-rows-per-page chunk.
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
import { mergeSeriesRowsForTable, seriesLabel } from "@/lib/reports/series";
import type {
  CanvasFilters,
  Dimension,
  TableWidget as TableWidgetType,
} from "@/lib/reports/types";

interface Props {
  widget: TableWidgetType;
  canvasFilters?: CanvasFilters;
}

const PAGE_SIZE = 50;

const DIMENSION_HEADERS: Record<Dimension, string> = {
  category: "Category",
  category_master: "Master category",
  account: "Account",
  tag: "Tag",
  txn_type: "Type",
  status: "Status",
  month: "Month",
  week: "Week",
  day: "Day",
};

export default function TableWidget({ widget, canvasFilters }: Props) {
  const measures = widget.config.measures.map((m) => m.measure);
  const { series, isLoading, error } = useSeriesQueries(
    widget,
    canvasFilters,
    measures,
  );

  const seriesKeys = widget.config.measures.map((_, i) => `s${i}`);
  const seriesLabels = widget.config.measures.map((m, i) =>
    seriesLabel(m, i, widget.config.measures.length),
  );
  const dimensions = widget.config.dimensions;
  const rows = mergeSeriesRowsForTable(series, dimensions, seriesKeys);

  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);

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

  const totalPages = Math.max(1, Math.ceil(sortedRows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pagedRows = sortedRows.slice(
    safePage * PAGE_SIZE,
    safePage * PAGE_SIZE + PAGE_SIZE,
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

  return (
    <div
      data-testid="table-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div
        className="mb-2 text-sm font-semibold text-text-primary"
        aria-label={widget.title || "Table"}
      >
        {widget.title || "Table"}
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
          </table>
        )}
      </div>
      {sortedRows.length > PAGE_SIZE && (
        <div
          data-testid="table-widget-pagination"
          className="mt-2 flex items-center justify-between text-xs text-text-muted"
        >
          <span>
            Page {safePage + 1} of {totalPages} · {sortedRows.length} rows
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              data-testid="table-widget-prev-page"
              disabled={safePage === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="rounded border border-border px-2 py-0.5 hover:bg-bg-elevated disabled:opacity-40"
            >
              Prev
            </button>
            <button
              type="button"
              data-testid="table-widget-next-page"
              disabled={safePage >= totalPages - 1}
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              className="rounded border border-border px-2 py-0.5 hover:bg-bg-elevated disabled:opacity-40"
            >
              Next
            </button>
          </div>
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
  if (format === "currency") {
    return n.toLocaleString(undefined, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    });
  }
  if (format === "percent") return `${n.toFixed(1)}%`;
  return n.toLocaleString();
}
