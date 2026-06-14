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
} {
  const { data, isLoading } = useSWR<SourceCatalogEntry[]>(
    SOURCES_SWR_KEY,
    fetchSources,
    { revalidateOnFocus: false },
  );
  return { sources: data ?? [], isLoading };
}
