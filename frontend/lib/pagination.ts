import { apiFetch } from "@/lib/api";

/**
 * Page through a list endpoint that supports `limit` + `offset` query
 * params and returns an array of T. Stops when a response shorter than
 * `pageSize` arrives. For all-time aggregates whose per-page cap (≤200
 * server-side) would otherwise truncate the result.
 *
 * Caller passes a base URL with any non-pagination query params already
 * attached, e.g. `fetchAll<Transaction>("/api/v1/transactions?status=pending")`.
 *
 * Lives in its own module (not `lib/api.ts`) so test code that mocks
 * `apiFetch` via `vi.mock("@/lib/api", ...)` correctly intercepts the
 * fetcher used here. Functions that call `apiFetch` from within
 * `lib/api.ts` itself bypass the export-level mock; cross-module imports
 * do not.
 */
export async function fetchAll<T>(baseUrl: string, pageSize = 200): Promise<T[]> {
  const result: T[] = [];
  let offset = 0;
  const sep = baseUrl.includes("?") ? "&" : "?";
  // Cap the loop at a sane upper bound. 100 × 200 = 20k items is far
  // beyond any realistic dashboard workload; raise if a real workload
  // ever approaches it.
  for (let page = 0; page < 100; page += 1) {
    const rows = await apiFetch<T[]>(`${baseUrl}${sep}limit=${pageSize}&offset=${offset}`);
    if (!Array.isArray(rows) || rows.length === 0) break;
    result.push(...rows);
    if (rows.length < pageSize) break;
    offset += pageSize;
  }
  return result;
}
