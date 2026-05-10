// Lightweight localStorage-backed persistence helpers used by the sort/filter
// hooks. Centralizing the JSON guard rails here keeps each hook small and the
// SSR-safe checks (`typeof window`) in exactly one spot. Item 6 of the punch
// list (sort persistence everywhere) and item 16 (Dashboard Spending sortable
// columns) both depend on this surface.
//
// Read failures (missing window, missing key, malformed JSON, validator
// rejection) all fall through to the supplied default rather than throwing, so
// a corrupted localStorage entry can never brick a page. Write failures are
// swallowed for the same reason: if the browser refuses to persist (private
// mode, quota), the in-memory state still works.

export function readPersisted<T>(
  key: string,
  fallback: T,
  validate?: (value: unknown) => value is T,
): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw == null) return fallback;
    const parsed = JSON.parse(raw) as unknown;
    if (validate && !validate(parsed)) return fallback;
    return parsed as T;
  } catch {
    return fallback;
  }
}

export function writePersisted<T>(key: string, value: T): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Quota or unavailable storage; ignore.
  }
}

export function clearPersisted(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignore.
  }
}
