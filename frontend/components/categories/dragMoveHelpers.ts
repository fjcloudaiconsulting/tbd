/**
 * Pure helpers for the C2b drag-and-drop subcategory move flow.
 *
 * The page's `onDragEnd` handler delegates to `classifyDrop` to decide
 * whether a drop should:
 *   - open the confirm modal ("valid"),
 *   - silently no-op ("source_parent" or "cross_type"),
 *   - or be ignored ("missing_data" / "wrong_kind").
 *
 * Keeping the classification pure makes the handler trivially
 * unit-testable without simulating real pointer / keyboard events in
 * jsdom.
 *
 * Owned by Team Categories C2 UI (C2b).
 */

import type { ApiResponseError } from "@/lib/api";

export type CategoryType = "income" | "expense" | "both";

export interface SubcategoryDragData {
  kind: "subcategory";
  subcategoryId: number;
  subcategoryName: string;
  subcategoryType: CategoryType;
  parentId: number;
}

export interface MasterDropData {
  kind: "master";
  masterId: number;
  masterType: CategoryType;
}

export type DropClassification =
  | { kind: "valid"; sub: SubcategoryDragData; target: MasterDropData }
  | { kind: "source_parent"; sub: SubcategoryDragData; target: MasterDropData }
  | { kind: "cross_type"; sub: SubcategoryDragData; target: MasterDropData }
  | { kind: "no_drop" }
  | { kind: "wrong_kind" };

interface RawData {
  kind?: string;
  subcategoryId?: number;
  subcategoryName?: string;
  subcategoryType?: CategoryType;
  parentId?: number;
  masterId?: number;
  masterType?: CategoryType;
}

function asSubcategoryData(data: unknown): SubcategoryDragData | null {
  if (!data || typeof data !== "object") return null;
  const d = data as RawData;
  if (d.kind !== "subcategory") return null;
  if (
    typeof d.subcategoryId !== "number"
    || typeof d.subcategoryName !== "string"
    || (d.subcategoryType !== "income" && d.subcategoryType !== "expense" && d.subcategoryType !== "both")
    || typeof d.parentId !== "number"
  ) {
    return null;
  }
  return {
    kind: "subcategory",
    subcategoryId: d.subcategoryId,
    subcategoryName: d.subcategoryName,
    subcategoryType: d.subcategoryType,
    parentId: d.parentId,
  };
}

function asMasterData(data: unknown): MasterDropData | null {
  if (!data || typeof data !== "object") return null;
  const d = data as RawData;
  if (d.kind !== "master") return null;
  if (
    typeof d.masterId !== "number"
    || (d.masterType !== "income" && d.masterType !== "expense" && d.masterType !== "both")
  ) {
    return null;
  }
  return {
    kind: "master",
    masterId: d.masterId,
    masterType: d.masterType,
  };
}

export function classifyDrop(
  activeData: unknown,
  overData: unknown | undefined,
): DropClassification {
  if (overData === undefined || overData === null) return { kind: "no_drop" };
  const sub = asSubcategoryData(activeData);
  const target = asMasterData(overData);
  if (!sub || !target) return { kind: "wrong_kind" };
  if (target.masterId === sub.parentId) {
    return { kind: "source_parent", sub, target };
  }
  if (target.masterType !== sub.subcategoryType) {
    return { kind: "cross_type", sub, target };
  }
  return { kind: "valid", sub, target };
}

interface CategoryErrorDetail {
  detail?: string;
  conflicting_child_name?: string;
  message?: string;
}

/**
 * Builds a user-facing error message from an ApiResponseError surfaced
 * by the move preview or PATCH endpoint. Handles the
 * `name_collision` (409) and `type_mismatch` (400) cases the C0
 * contract spells out, with sensible fallbacks for anything else.
 */
export function buildMoveErrorMessage(
  err: unknown,
  subName: string,
  targetName: string,
): string {
  if (err instanceof Error) {
    const apiErr = err as ApiResponseError;
    const detail = apiErr.detail as CategoryErrorDetail | string | undefined;
    if (typeof detail === "object" && detail !== null) {
      if (detail.detail === "name_collision" && detail.conflicting_child_name) {
        return `Cannot move "${subName}" to "${targetName}": a subcategory named "${detail.conflicting_child_name}" already exists there. Rename one before moving.`;
      }
      if (detail.detail === "type_mismatch") {
        return `Cannot move "${subName}" to "${targetName}": types are incompatible.`;
      }
      if (typeof detail.detail === "string") {
        return `Move failed: ${detail.detail}`;
      }
    }
    return err.message || `Move failed for "${subName}".`;
  }
  return `Move failed for "${subName}".`;
}
