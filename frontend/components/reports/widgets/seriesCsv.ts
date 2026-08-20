/**
 * Shared CSV export-dataset builder for the multi-series chart widgets
 * (Line / Area). Both render the same merged shape:
 * one row per dimension value with ``label`` plus one numeric field per
 * series key (``s0..sN``). The CSV mirrors that: a dimension column
 * followed by one column per series, using the human series labels as
 * headers.
 *
 * ⚠ ``stacked_bar`` no longer uses this. TBD-382 made it a single-measure
 * widget that stacks by ``dimensions[1]``, so it renders through BarWidget
 * and exports one column per SECONDARY VALUE — see ``lib/reports/breakdown``.
 */
import { dimensionHeader } from "@/lib/reports/series";
import type { CsvCell, CsvDataset } from "@/lib/reports/csv";

export function buildSeriesCsvDataset(
  dimensionKey: string,
  rows: Array<{ label: string } & Record<string, number | string>>,
  seriesKeys: string[],
  seriesLabels: string[],
): CsvDataset {
  return {
    headers: [dimensionHeader(dimensionKey), ...seriesLabels],
    rows: rows.map((r) => [
      String(r.label),
      ...seriesKeys.map((key) =>
        typeof r[key] === "number" ? (r[key] as number) : 0,
      ),
    ]) as CsvCell[][],
  };
}
