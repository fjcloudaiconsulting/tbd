/**
 * Sankey node-label mapping — shared by the chart (node labels) and the CSV
 * export so both show the friendly "Income" rather than the raw
 * "__hub_income__" sentinel id.
 *
 * Kept in a tiny standalone module (no @nivo/sankey import) so the CSV path
 * in ``SankeyWidget`` can reuse it without pulling the code-split chart bundle
 * into the main chunk.
 *
 * Hub/sentinel id → friendly display label. Real category node ids are not
 * present here; their label === id.
 */
export const HUB_LABELS: Record<string, string> = {
  __hub_income__: "Income",
  __hub_savings__: "Savings",
  __hub_other__: "Other",
};

/**
 * Friendly display label for a Sankey node id: hub sentinels map to their
 * label, real category ids pass through unchanged.
 */
export function sankeyNodeLabel(id: string): string {
  return HUB_LABELS[id] ?? id;
}
