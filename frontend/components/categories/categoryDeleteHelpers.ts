import { ApiResponseError, extractErrorMessage } from "@/lib/api";
import type { Category } from "@/lib/types";

export interface FailureRow {
  category_id: number;
  category_name: string;
  reason: string;
  reason_code?: string;
}

export interface CategoryDeleteResult {
  deleted_category_id: number;
  migration_target_id: number | null;
  migrated_transaction_count: number;
  migrated_recurring_count: number;
  migrated_forecast_item_count: number;
  migrated_rule_count: number;
  deleted_rule_count: number;
}

interface CategoryErrorDetail {
  detail?: string;
  conflicting_child_name?: string;
  scope?: string;
  type?: string;
  child_names?: string[];
  source_type?: string;
  target_type?: string;
  dependent_breakdown?: { income: number; expense: number };
}

/**
 * Compatible migration targets for a subcategory, mirroring the backend
 * rule at ``_check_target_compatibility_for_delete``: INCOME source -> INCOME
 * or BOTH master; EXPENSE source -> EXPENSE or BOTH master; BOTH source ->
 * BOTH master (the safe default for the breakdown-driven backend rule).
 *
 * ``excludeIds`` lets callers drop other rows about to be deleted (batch
 * flow) so the user cannot pick a soon-to-be-gone category as the target.
 * Masters only (parent_id === null); the source itself is always excluded.
 */
export function compatibleTargets(
  sub: Category,
  categories: Category[],
  excludeIds: Set<number> = new Set(),
): Category[] {
  return categories.filter((c) => {
    if (c.id === sub.id) return false;
    if (c.parent_id !== null) return false;
    if (excludeIds.has(c.id)) return false;
    if (sub.type === "income") return c.type === "income" || c.type === "both";
    if (sub.type === "expense") return c.type === "expense" || c.type === "both";
    return c.type === "both";
  });
}

/**
 * Map a delete failure (typically a 409/422 from the C0 delete contract)
 * to a human-readable reason. Shared by the batch- and single-delete flows
 * so the messaging stays identical: has_children, last_in_type, type_mismatch,
 * name_collision, migration_target_required.
 */
export function buildFailure(sub: Category, err: unknown): FailureRow {
  if (err instanceof ApiResponseError) {
    const detail = err.detail as CategoryErrorDetail | string | undefined;
    if (typeof detail === "object" && detail !== null) {
      if (detail.detail === "last_in_type") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Cannot delete the only ${detail.type ?? ""} ${detail.scope ?? "subcategory"}.`,
          reason_code: "last_in_type",
        };
      }
      if (detail.detail === "has_children") {
        const sample = detail.child_names?.[0] ?? "subcategory";
        const more = (detail.child_names?.length ?? 0) - 1;
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Has subcategories. Move or delete "${sample}"${
            more > 0 ? ` and ${more} other${more === 1 ? "" : "s"}` : ""
          } first.`,
          reason_code: "has_children",
        };
      }
      if (detail.detail === "type_mismatch") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Migration target type ${detail.target_type ?? ""} is not compatible with ${detail.source_type ?? "source"}.`,
          reason_code: "type_mismatch",
        };
      }
      if (detail.detail === "name_collision") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Name collision: "${detail.conflicting_child_name ?? sub.name}" already exists on the target.`,
          reason_code: "name_collision",
        };
      }
      if (detail.detail === "migration_target_required") {
        return {
          category_id: sub.id,
          category_name: sub.name,
          reason: `Migration target required.`,
          reason_code: "migration_target_required",
        };
      }
    }
    return {
      category_id: sub.id,
      category_name: sub.name,
      reason: err.message || "Delete failed",
    };
  }
  return {
    category_id: sub.id,
    category_name: sub.name,
    reason: extractErrorMessage(err, "Delete failed"),
  };
}

/** Whether an ApiResponseError is the 422 migration_target_required signal. */
export function isMigrationTargetRequired(err: unknown): boolean {
  if (!(err instanceof ApiResponseError)) return false;
  const detail = err.detail as CategoryErrorDetail | string | undefined;
  return (
    typeof detail === "object" &&
    detail !== null &&
    detail.detail === "migration_target_required"
  );
}
