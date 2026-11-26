import type {
  DonutDatum,
  SpendingCategoryRow,
} from "@/components/dashboard/DashboardDataProvider";

/**
 * Donut slices from the server rows: each row summed under its master
 * (`parent_id ?? category_id`), named `parent_name ?? category_name` so a
 * master with no direct spend still gets its name. Categories are two levels
 * deep and budgets live on masters, so this is the grouping Budget Progress
 * uses (`budget_service._compute_spent`). Shared by both dashboard shells
 * (TBD-339); it lives outside the provider so tests that mock the provider
 * module still render the legacy shell's donut.
 */
export function donutSlices(
  rows: SpendingCategoryRow[] | null | undefined,
): DonutDatum[] {
  if (!Array.isArray(rows)) return [];
  const byMaster = new Map<number, DonutDatum>();
  for (const r of rows) {
    const id = r.parent_id ?? r.category_id;
    const slice = byMaster.get(id) ?? {
      categoryId: id,
      name: r.parent_name ?? r.category_name,
      value: 0,
    };
    slice.value += Number(r.executed);
    byMaster.set(id, slice);
  }
  return [...byMaster.values()]
    // A defensive finite/positive guard, NOT a negative-expense handler: no
    // reachable state produces one. `TransactionCreate/Update.amount` and
    // `ImportConfirmRow.amount` are `Field(gt=0)`, the OFX path writes
    // `amount_abs`, sign is carried by `type`, and the rollup groups only
    // `type == EXPENSE` — so a refund is an INCOME row, never a negative
    // EXPENSE, and every group sum is strictly positive.
    .filter((d) => Number.isFinite(d.value) && d.value > 0)
    .sort((a, b) => b.value - a.value);
}
