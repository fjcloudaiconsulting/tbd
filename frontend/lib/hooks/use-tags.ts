"use client";

/**
 * Shared SWR hook for the org's tags (SWR Phase 2 reference-data
 * migration). See ``use-accounts`` for the bare-path-key + no-focus-
 * revalidation rationale.
 */
import useSWR from "swr";

import { apiFetch } from "@/lib/api";

/**
 * Tag suggestion shape returned by ``GET /api/v1/tags``. Kept here as the
 * single definition so every consumer (e.g. the reports ``TagFilter``)
 * shares one type rather than re-declaring the response inline.
 */
export interface TagResponse {
  id: number;
  name: string;
  name_normalized: string;
  usage_count: number;
}

export const TAGS_KEY = "/api/v1/tags";

/** ``enabled`` gates the fetch until auth resolves; see ``use-accounts``. */
export function useTags(enabled: boolean = true) {
  return useSWR<TagResponse[]>(
    enabled ? TAGS_KEY : null,
    () => apiFetch<TagResponse[]>(TAGS_KEY),
    { revalidateOnFocus: false },
  );
}
