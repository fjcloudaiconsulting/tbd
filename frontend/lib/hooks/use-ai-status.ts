"use client";

import useSWR from "swr";
import { apiFetch } from "@/lib/api";
import type { AIStatus } from "@/lib/types";

export function useAiStatus() {
  // SWR dedupes so all surfaces share one request. AI is opt-in/non-critical,
  // so failures resolve to undefined (surfaces hide rather than error).
  const { data } = useSWR<AIStatus>("/api/v1/ai/status", (url: string) => apiFetch<AIStatus>(url), {
    shouldRetryOnError: false,
    revalidateOnFocus: false,
    // Surfaces hide on failure; log so a broken /ai/status is triageable in prod.
    onError: (err) =>
      console.warn("useAiStatus: /api/v1/ai/status failed; AI surfaces hidden", err),
  });
  return data;
}
