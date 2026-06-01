"use client";

/**
 * Per-widget "Export CSV" affordance.
 *
 * Each widget already derives the exact rows it renders (single-measure
 * widgets from ``useReportQuery``; multi-series / table widgets from the
 * client-side merge of ``useSeriesQueries``). Rather than route a fresh
 * backend export endpoint, the widget passes that in-memory display data
 * down here as a ``{ headers, rows }`` dataset and this button serializes
 * + downloads it client-side.
 *
 * Lives inside each widget's own header row (not in ``WidgetShell``)
 * because the rows are computed inside the widget body from hooks that
 * ``WidgetShell`` — which only wraps the rendered children — can't see.
 * Keeping the button local to where the data is derived is the least
 * invasive integration and stays consistent across all eight widgets.
 *
 * View-mode only: hidden in edit mode to keep the editing chrome (drag
 * handle, remove, config rail) uncluttered.
 */
import { Download } from "lucide-react";

import { csvFilename, downloadCsv, toCsv, type CsvDataset } from "@/lib/reports/csv";

interface Props {
  /** Widget title; slugified into the download filename. */
  title: string;
  /** The exact rows the widget is displaying. */
  dataset: CsvDataset;
  /** Hidden in edit mode. Defaults to view mode (false). */
  editMode?: boolean;
}

export default function WidgetCsvButton({ title, dataset, editMode }: Props) {
  if (editMode) return null;

  const hasRows = dataset.rows.length > 0;

  function handleExport() {
    if (!hasRows) return;
    const csv = toCsv(dataset.headers, dataset.rows);
    downloadCsv(csvFilename(title), csv);
  }

  return (
    <button
      type="button"
      data-testid="widget-csv-export"
      onClick={(e) => {
        // The widget shell selects the widget on click; exporting
        // shouldn't change selection.
        e.stopPropagation();
        handleExport();
      }}
      disabled={!hasRows}
      title="Export CSV"
      aria-label={`Export ${title || "widget"} as CSV`}
      className="rounded p-1 text-text-muted transition hover:bg-bg-elevated hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
    >
      <Download aria-hidden="true" className="h-3.5 w-3.5" />
    </button>
  );
}
