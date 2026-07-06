"use client";

/**
 * Shared SWR hook for the org's billing periods (SWR Phase 2 reference-
 * data migration). See ``use-accounts`` for the bare-path-key + no-focus-
 * revalidation rationale.
 *
 * Note the endpoint lives under ``/api/v1/settings/billing-periods`` (not
 * a top-level resource), so the cache key mirrors that path.
 */
import useSWR from "swr";

import { apiFetch } from "@/lib/api";
import type { BillingPeriod } from "@/lib/types";

export const BILLING_PERIODS_KEY = "/api/v1/settings/billing-periods";

/** ``enabled`` gates the fetch until auth resolves; see ``use-accounts``. */
export function useBillingPeriods(enabled: boolean = true) {
  return useSWR<BillingPeriod[]>(
    enabled ? BILLING_PERIODS_KEY : null,
    () => apiFetch<BillingPeriod[]>(BILLING_PERIODS_KEY),
    { revalidateOnFocus: false },
  );
}
