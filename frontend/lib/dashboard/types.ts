/**
 * Dashboard layout — frontend types.
 *
 * Mirrors the backend ``DashboardLayout`` Pydantic schema from
 * ``backend/app/schemas/dashboard.py``. Reuses the shared canvas
 * types from the Reports surface so layout_json and canvas_filters_json
 * are always structurally compatible across both features.
 */

import type { CanvasFilters, LayoutJson } from "@/lib/reports/types";

export type { CanvasFilters, LayoutJson };

/**
 * Wire shape returned by GET /api/v1/dashboard and
 * PATCH /api/v1/dashboard. The backend auto-creates a default layout
 * on the first GET if none exists yet.
 */
export interface DashboardLayoutResponse {
  id: number;
  owner_user_id: number;
  org_id: number;
  layout_json: LayoutJson | Record<string, never>;
  canvas_filters_json: CanvasFilters | Record<string, never>;
  schema_version: number;
  created_at: string;
  updated_at: string;
}
