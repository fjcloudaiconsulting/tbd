"use client";

/**
 * `useReportSources` — fetches the self-describing data-source catalog
 * from `GET /api/v1/reports/sources` (the registry the backend Phase 5
 * work exposed). Each entry declares the dimensions, measures, and
 * filters a source supports; the widget editor's Data tab drives its
 * pickers off the selected source's catalog so a widget can never offer
 * (and then 422 on) an out-of-source field.
 *
 * Mirrors the `AccountFilter` fetch idiom: `apiFetch` through SWR with
 * `revalidateOnFocus: false`. `sources` defaults to `[]` so callers can
 * render gracefully while loading.
 */
import useSWR from "swr";

import { apiFetch } from "@/lib/api";
import type { SourceCatalogEntry } from "@/lib/reports/types";

const SOURCES_SWR_KEY = "/api/v1/reports/sources";

async function fetchSources(): Promise<SourceCatalogEntry[]> {
  return apiFetch<SourceCatalogEntry[]>("/api/v1/reports/sources");
}

export function useReportSources(): {
  sources: SourceCatalogEntry[];
  isLoading: boolean;
  /**
   * The fetch's failure, surfaced rather than swallowed (TBD-403 part 1,
   * landed with TBD-430 which is its first real consumer).
   *
   * ⚠ Without this, "the catalog is DOWN" and "the catalog is EMPTY" are
   * byte-identical to every caller: both are `{sources: [], isLoading:
   * false}`. They are not the same fact. With `sources: []`,
   * `sourceSupportsField` returns `true` by design, so widgets push
   * filters their source does not publish and take a 422 — the canvas
   * fills with "Couldn't load" for a reason no widget can name. The
   * report pages render ONE page-scoped banner off this; `/reports/
   * sources` is a single constant SWR key, so its failure is one fact
   * about the whole canvas and never a per-widget notice.
   */
  error: Error | undefined;
} {
  const { data, isLoading, error } = useSWR<SourceCatalogEntry[]>(
    SOURCES_SWR_KEY,
    fetchSources,
    { revalidateOnFocus: false },
  );
  return {
    sources: data ?? [],
    isLoading,
    error: (error as Error | undefined) ?? undefined,
  };
}
