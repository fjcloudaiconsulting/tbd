/**
 * Safe accessor for the tour "pending start" flag.
 *
 * The onboarding wizard's "Yes, show me" path, the AppShell "Replay
 * product tour" menu item, and the settings RestartTourCard all stage a
 * one-shot flag that DashboardTourAutoStart consumes on the next
 * /dashboard mount. That flag lived in sessionStorage behind a bare
 * try/catch. sessionStorage THROWS (or is unavailable) in Safari private
 * mode, with cookies / site data disabled, and on quota errors — the bare
 * catch swallowed the failure, so in those environments the flag write
 * silently vanished. The first-run onboarding path is the sharp edge: it
 * has no TourContext pending-start fallback, so a swallowed write meant
 * the tour never started, and a flag that could not be cleared could
 * re-trigger.
 *
 * These helpers try web storage first (so cross-reload persistence is
 * UNCHANGED when storage works) and fall back to a module-scoped
 * in-memory map when storage is unavailable or throws. The in-memory map
 * keeps the flag consistent for the rest of the current SPA session: a
 * staged tour still starts after a client navigation, and clearing it
 * prevents a repeat trigger.
 *
 * LIMITATION: the in-memory fallback is per page load. When storage is
 * disabled the flag cannot survive a FULL page reload — that is
 * physically impossible without persistent storage. The goal here is
 * within-session consistency and never crashing the app, not cross-reload
 * persistence.
 */

// Module-scoped fallback. Keyed by the same string used for sessionStorage
// so a value stays readable regardless of which backend holds it.
const memoryFlags = new Map<string, string>();

/**
 * Read a tour flag. Prefers real sessionStorage (unchanged happy path);
 * falls back to the in-memory map when storage is missing, throws, or has
 * no value. Never throws.
 */
export function safeTourStorageGet(key: string): string | null {
  try {
    const value = window.sessionStorage.getItem(key);
    if (value !== null) return value;
  } catch {
    // sessionStorage unavailable / throwing — fall through to memory.
  }
  return memoryFlags.has(key) ? (memoryFlags.get(key) as string) : null;
}

/**
 * Write a tour flag. Always records the value in memory first so a later
 * read succeeds even if the sessionStorage write throws, then attempts the
 * persistent write. Never throws.
 */
export function safeTourStorageSet(key: string, value: string): void {
  memoryFlags.set(key, value);
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // sessionStorage unavailable — the in-memory copy carries it this
    // session (until a full reload, which storage-disabled clients cannot
    // survive anyway).
  }
}

/**
 * Clear a tour flag from both backends so a consumed / dismissed tour
 * stays dismissed for the session and cannot re-trigger. Never throws.
 */
export function safeTourStorageRemove(key: string): void {
  memoryFlags.delete(key);
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // sessionStorage unavailable — nothing persisted there to clear.
  }
}
