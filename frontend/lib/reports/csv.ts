/**
 * Client-side CSV export for Reports v2 widgets.
 *
 * Each widget already holds the exact rows it rendered (from
 * ``useReportQuery`` / ``useSeriesQueries`` merged client-side). Rather
 * than round-trip a new backend endpoint, a widget builds a small
 * ``{ headers, rows }`` export dataset from that in-memory data and
 * hands it to ``downloadCsv`` to trigger a browser download.
 */

/** A field value in a CSV cell. ``null``/``undefined`` render empty. */
export type CsvCell = string | number | null | undefined;

export interface CsvDataset {
  headers: string[];
  rows: CsvCell[][];
}

/**
 * Serialize headers + rows to an RFC4180-ish CSV string.
 *
 * Fields containing a comma, double-quote, CR or LF are wrapped in
 * double quotes; interior double-quotes are doubled. Numbers render via
 * ``String(n)``; ``null``/``undefined`` render as an empty field. Rows
 * are joined with CRLF (the RFC4180 record separator); no trailing
 * newline is appended.
 */
export function toCsv(headers: string[], rows: CsvCell[][]): string {
  const lines = [headers.map(escapeField).join(",")];
  for (const row of rows) {
    lines.push(row.map(escapeField).join(","));
  }
  return lines.join("\r\n");
}

function escapeField(value: CsvCell): string {
  if (value === null || value === undefined) return "";
  const s = typeof value === "number" ? String(value) : String(value);
  if (/[",\r\n]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

/**
 * Trigger a browser download of ``csv`` as ``filename`` via a Blob +
 * object URL and a synthesized anchor click. No-ops outside the browser
 * (SSR / non-DOM environments).
 */
export function downloadCsv(filename: string, csv: string): void {
  if (typeof document === "undefined" || typeof URL === "undefined") return;
  // Prepend a UTF-8 BOM so Excel opens accented characters correctly.
  const blob = new Blob(["﻿" + csv], {
    type: "text/csv;charset=utf-8;",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

/**
 * Slugify a widget title into a safe CSV filename stem. Falls back to
 * ``"report"`` when the title is empty after slugification.
 */
export function csvFilename(title: string): string {
  const slug = title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${slug || "report"}.csv`;
}
