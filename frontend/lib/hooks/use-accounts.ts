"use client";

/**
 * Shared SWR hook for the org's accounts (SWR Phase 2 reference-data
 * migration). Bare-path cache key: on org-switch the app does a full
 * reload (the product decision), so an org-scoped key is unnecessary and
 * the bare path lets every consumer dedupe onto one request/cache entry.
 *
 * ``revalidateOnFocus`` is off to match the pre-SWR behavior (refs were
 * fetched once on mount and only re-pulled on an explicit event, e.g. a
 * transaction-add). Callers force a refresh with the returned ``mutate``.
 */
import useSWR from "swr";

import { apiFetch } from "@/lib/api";
import type { Account } from "@/lib/types";

export const ACCOUNTS_KEY = "/api/v1/accounts";

/**
 * ``enabled`` gates the fetch (a null SWR key means "don't fetch yet"),
 * mirroring the pre-SWR guard that only pulled refs once auth had
 * resolved (``!loading && user``). Fetching before the bearer token is
 * set would 401/403 (the auth-race class). ``mutate`` bound to a null key
 * is a no-op, and rebinds to the real key once ``enabled`` flips true.
 */
export function useAccounts(enabled: boolean = true) {
  return useSWR<Account[]>(
    enabled ? ACCOUNTS_KEY : null,
    () => apiFetch<Account[]>(ACCOUNTS_KEY),
    { revalidateOnFocus: false },
  );
}
