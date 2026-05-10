"use client";

import { useEffect, useRef } from "react";

/**
 * Subscribes to the `pfv:transaction-added` window event and invokes
 * the supplied reload function whenever the AppShell-level CTA reports
 * a successful save.
 *
 * The CTA dispatches the event from a shared header (one button across
 * /dashboard, /transactions, /accounts, /forecast-plans, /budgets, ...)
 * rather than holding a callback ref to whichever page the user is on.
 * Each affected page subscribes here and reloads its own data without
 * prop drilling or RSC navigation, which would skip the in-flight
 * client-side useEffect fetches the page already runs.
 *
 * The reload argument is held in a ref so callers can pass an inline
 * arrow (or a fresh `useCallback` whose deps change with filters) and
 * the listener still calls the latest version, without re-subscribing
 * on every render. Re-subscribing each render would tear down and
 * re-add the global listener constantly during typical filter-driven
 * pages, briefly missing events that fire between unbind and rebind.
 */
export function useTransactionAddedListener(reload: () => void): void {
  const reloadRef = useRef(reload);

  useEffect(() => {
    reloadRef.current = reload;
  }, [reload]);

  useEffect(() => {
    function handler() {
      reloadRef.current();
    }
    window.addEventListener("pfv:transaction-added", handler);
    return () => {
      window.removeEventListener("pfv:transaction-added", handler);
    };
  }, []);
}
