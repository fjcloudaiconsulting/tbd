"use client";

/**
 * Shared SWR hook for the org's categories (SWR Phase 2 reference-data
 * migration). See ``use-accounts`` for the bare-path-key + no-focus-
 * revalidation rationale.
 *
 * Consumers that create a category inline should optimistically prepend
 * it via ``mutate`` (``mutate([...current, cat], { revalidate: false })``)
 * so the new option appears immediately, mirroring the pre-SWR local
 * ``setCategories`` append.
 */
import useSWR from "swr";

import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

export const CATEGORIES_KEY = "/api/v1/categories";

/** ``enabled`` gates the fetch until auth resolves; see ``use-accounts``. */
export function useCategories(enabled: boolean = true) {
  return useSWR<Category[]>(
    enabled ? CATEGORIES_KEY : null,
    () => apiFetch<Category[]>(CATEGORIES_KEY),
    { revalidateOnFocus: false },
  );
}
