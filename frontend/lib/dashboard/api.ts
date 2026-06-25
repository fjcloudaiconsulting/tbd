/**
 * Typed client for the Dashboard REST surface.
 *
 * Goes through ``apiFetch`` so it inherits Bearer auth, silent
 * refresh, and the proactive-refresh preflight. The dashboard
 * endpoint auto-creates a default layout on the first GET if none
 * exists yet for the caller's org.
 *
 * The PATCH endpoint uses ``extra="forbid"`` on the backend, so only
 * ``layout_json`` and ``canvas_filters_json`` may appear in the wire
 * body — ``saveDashboard`` enforces this by construction.
 */
import { apiFetch } from "@/lib/api";
import type { CanvasFilters, DashboardLayoutResponse, LayoutJson } from "./types";

/**
 * Fetch the caller's org dashboard layout. Auto-creates a default
 * layout server-side on first call if none exists yet.
 */
export async function getDashboard(): Promise<DashboardLayoutResponse> {
  return apiFetch<DashboardLayoutResponse>("/api/v1/dashboard");
}

/**
 * Fetch the canonical default layout from the server WITHOUT persisting
 * anything. Used by the Reset-to-default action in Customize mode so
 * the frontend never needs to duplicate the seed.
 */
export async function getDefaultDashboard(): Promise<{
  layout_json: LayoutJson;
  canvas_filters_json: CanvasFilters;
}> {
  return apiFetch<{ layout_json: LayoutJson; canvas_filters_json: CanvasFilters }>(
    "/api/v1/dashboard/default",
  );
}

/**
 * Persist updated layout and canvas filters. Sends ONLY the two
 * accepted keys — the backend uses ``extra="forbid"`` and will 422
 * any additional fields.
 */
export async function saveDashboard(
  layout_json: LayoutJson | Record<string, never>,
  canvas_filters_json: CanvasFilters | Record<string, never>,
): Promise<DashboardLayoutResponse> {
  return apiFetch<DashboardLayoutResponse>("/api/v1/dashboard", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layout_json, canvas_filters_json }),
  });
}
