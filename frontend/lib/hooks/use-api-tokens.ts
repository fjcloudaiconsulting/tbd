"use client";

/**
 * Shared SWR hook for the superadmin's PATs (spec
 * `specs/2026-07-21-superadmin-api-tokens-design.md`). Bare-path cache key so
 * every consumer dedupes onto one request/cache entry, mirroring the SWR
 * Phase 2 reference-data hooks (`use-accounts` et al.).
 *
 * `revalidateOnFocus` is off to match the reference-data idiom; callers force
 * a refresh with the returned `mutate` after a mint or revoke.
 */
import useSWR from "swr";

import { listApiTokens } from "@/lib/api-tokens";
import type { ApiToken, ListEnvelope } from "@/lib/types";

export const API_TOKENS_KEY = "/api/v1/system/api-tokens";

/**
 * `enabled` gates the fetch (a null SWR key means "don't fetch yet"),
 * mirroring the auth-race guard the other reference hooks use — fetching
 * before the bearer token is set would 401/403.
 */
export function useApiTokens(enabled: boolean = true) {
  return useSWR<ListEnvelope<ApiToken>>(
    enabled ? API_TOKENS_KEY : null,
    () => listApiTokens(),
    { revalidateOnFocus: false },
  );
}
