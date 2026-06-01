/**
 * Typed client for the Reports v2 REST surface.
 *
 * Goes through ``apiFetch`` so it inherits Bearer auth, silent
 * refresh, and the proactive-refresh preflight. Calling these
 * functions while ``FEATURE_REPORTS_V2`` is off backend-side will
 * surface a 404 (the router dependency returns Not Found before the
 * route handlers run) — callers should treat 404 here the same as
 * "feature disabled."
 */
import { apiFetch } from "@/lib/api";
import type {
  CanvasFilters,
  LayoutJson,
  ReportCreatePayload,
  ReportSummary,
  ReportTemplate,
  ReportUpdatePayload,
  ReportVersionSummary,
  ReportsQuery,
  ReportsQueryResponse,
} from "./types";

export async function listReports(): Promise<ReportSummary[]> {
  return apiFetch<ReportSummary[]>("/api/v1/reports");
}

export async function listTemplates(): Promise<ReportTemplate[]> {
  return apiFetch<ReportTemplate[]>("/api/v1/reports/templates");
}

export async function getReport(id: number): Promise<ReportSummary> {
  return apiFetch<ReportSummary>(`/api/v1/reports/${id}`);
}

export async function createReport(
  payload: ReportCreatePayload,
): Promise<ReportSummary> {
  return apiFetch<ReportSummary>("/api/v1/reports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function createFromTemplate(
  t: ReportTemplate,
): Promise<ReportSummary> {
  return createReport({
    name: t.name,
    visibility: "private",
    layout_json: t.layout_json,
    canvas_filters_json: t.canvas_filters_json,
  });
}

export async function updateReport(
  id: number,
  payload: ReportUpdatePayload,
): Promise<ReportSummary> {
  return apiFetch<ReportSummary>(`/api/v1/reports/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteReport(id: number): Promise<void> {
  await apiFetch<void>(`/api/v1/reports/${id}`, { method: "DELETE" });
}

/**
 * Duplicate a report. The backend creates a fresh private copy owned
 * by the caller (regardless of who owns the source) and returns its
 * ``ReportSummary`` with a 201. Anyone who can view a report can
 * duplicate it.
 */
export async function duplicateReport(id: number): Promise<ReportSummary> {
  return apiFetch<ReportSummary>(`/api/v1/reports/${id}/duplicate`, {
    method: "POST",
  });
}

/**
 * Revert a report's live layout + canvas filters back to the
 * as-created snapshot the backend captured at creation time. Returns
 * the updated ``ReportSummary`` so the editor can re-hydrate its
 * canvas from the rolled-back server state.
 */
export async function resetReport(id: number): Promise<ReportSummary> {
  return apiFetch<ReportSummary>(`/api/v1/reports/${id}/reset`, {
    method: "POST",
  });
}

/**
 * List the saved versions of a report, newest-first. The original
 * snapshot carries ``is_original: true``; the editor's History panel
 * badges it and offers a Restore action per row.
 */
export async function listVersions(
  id: number,
): Promise<ReportVersionSummary[]> {
  return apiFetch<ReportVersionSummary[]>(`/api/v1/reports/${id}/versions`);
}

/**
 * Restore a saved version into the report's live layout + canvas
 * filters. Returns the updated ``ReportSummary`` so the editor can
 * re-hydrate its canvas from the restored server state.
 */
export async function restoreVersion(
  id: number,
  versionId: number,
): Promise<ReportSummary> {
  return apiFetch<ReportSummary>(
    `/api/v1/reports/${id}/versions/${versionId}/restore`,
    { method: "POST" },
  );
}

export async function runQuery(
  body: ReportsQuery,
): Promise<ReportsQueryResponse> {
  return apiFetch<ReportsQueryResponse>("/api/v1/reports/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/**
 * Save just the layout + canvas filters for an existing report. Thin
 * wrapper around ``updateReport`` so save handlers in the editor
 * read naturally.
 */
export async function saveLayout(
  id: number,
  layout_json: LayoutJson,
  canvas_filters_json: CanvasFilters,
): Promise<ReportSummary> {
  return updateReport(id, { layout_json, canvas_filters_json });
}
