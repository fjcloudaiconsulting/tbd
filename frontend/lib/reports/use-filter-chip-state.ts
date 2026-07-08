"use client";

/**
 * Shared filter-chip wiring for the two reports editors
 * (``/reports/[id]`` and ``/reports/new``). Both pages need the same
 * boilerplate to drive the per-widget filter chips:
 *
 *  - the accounts + categories SWR fetches (the shared bare-path
 *    ``useAccounts`` / ``useCategories`` hooks ``AccountFilter`` /
 *    ``CategoryPicker`` use, so the cache is shared and ids resolve to
 *    names in the chip header without an extra round-trip),
 *  - the ``requestedTab`` deep-link state (a chip click requests the
 *    popover's Filters tab; consume-and-clear resets it to null),
 *  - ``selectWidgetFilters`` (select the widget AND request Filters),
 *  - ``clearRequestedTab`` (used by the page's ``closePopover``).
 *
 * The page still owns ``selectedWidgetId`` (its selection model differs:
 * the ``[id]`` page is editMode-gated, the draft is always-edit), so it
 * passes ``setSelectedWidgetId`` in and the hook composes the chip-click
 * handler around it. Behavior is identical to the prior inlined code on
 * both pages.
 */
import { useCallback, useState } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import { useAccounts } from "@/lib/hooks/use-accounts";
import { useCategories } from "@/lib/hooks/use-categories";
import type { TabKey } from "@/components/reports/WidgetEditorPopover";
import type { Account, Category } from "@/lib/types";

interface FilterChipState {
  /** Accounts for chip name lookups (``[]`` while warming → count fallback). */
  accounts: Account[];
  /** Categories for chip name lookups (``[]`` while warming → count fallback). */
  categories: Category[];
  /** Deep-link request for the popover's Filters tab, or null. */
  requestedTab: TabKey | null;
  /** Select the widget AND request the popover's Filters tab. */
  selectWidgetFilters: (widgetId: string) => void;
  /** Clear any pending tab request (consume-and-clear / on popover close). */
  clearRequestedTab: () => void;
}

export function useFilterChipState(
  setSelectedWidgetId: (id: string) => void,
): FilterChipState {
  // Gate the SWR fetches on auth-readiness. AuthProvider sets the access
  // token BEFORE it exposes `user`, so `user` being present guarantees the
  // in-memory bearer is set. A null key means SWR does NOT fetch — this stops
  // the dashboard's mount-time chip fetches from firing token-less during the
  // cold-start hydration race (the dashboard mounts this hook above AppShell's
  // `user` gate, so without this guard `/accounts` + `/categories` go out bare
  // and 403). On the reports editors `user` is already present when this hook
  // mounts, so the key is immediately live — behavior there is unchanged.
  const { user } = useAuth();
  // Reuse the shared bare-path reference-data hooks ``AccountFilter`` /
  // ``CategoryPicker`` use so the cache is shared (no extra network
  // round-trip). Default to [] (count-fallback) while warm.
  const { data: accounts } = useAccounts(!!user);
  const { data: categories } = useCategories(!!user);

  // Deep-link request: a filter-chip click sets this to "filters" so the
  // popover opens on its Filters tab. Cleared the instant the popover
  // consumes it (consume-and-clear handshake).
  const [requestedTab, setRequestedTab] = useState<TabKey | null>(null);

  // Chip click: select the widget AND request the Filters tab. Stable
  // identity so the popover's effects don't re-run on every parent render.
  const selectWidgetFilters = useCallback(
    (widgetId: string) => {
      setSelectedWidgetId(widgetId);
      setRequestedTab("filters");
    },
    [setSelectedWidgetId],
  );

  const clearRequestedTab = useCallback(() => setRequestedTab(null), []);

  return {
    accounts: accounts ?? [],
    categories: categories ?? [],
    requestedTab,
    selectWidgetFilters,
    clearRequestedTab,
  };
}
