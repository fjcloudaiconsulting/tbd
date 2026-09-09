"use client";

/**
 * The currency every money figure is rendered in (TBD-503).
 *
 * ⚠ CONTEXT, NOT A FETCH. `useOrgCurrency` reads a value the app shell
 * supplies; it never requests anything itself. Money is formatted in leaf
 * components — modals, tiles, chart tooltips — and a leaf must not cause a
 * network call, throw without a provider, or perturb another hook's cache.
 *
 * Three earlier shapes were built and rejected, each by a real failure:
 *
 *  1. `useAuth()` to gate a fetch. `useAuth` THROWS outside a provider, so
 *     every money-rendering component suddenly required an AuthProvider.
 *     **257 tests failed.**
 *  2. An optional auth read. Broke the 22 suites whose `vi.mock` factory for
 *     AuthProvider does not export the optional reader.
 *  3. A cache-only `useSWR(KEY, null)`. Looked ideal — no request, no
 *     coupling — but a null-fetcher subscriber SUPPRESSES SWR's revalidation
 *     of that key for the page's own hook, so a transaction-added refresh
 *     silently stopped refetching accounts. Caught by
 *     `pages-reload-on-transaction-added.test.tsx`, which counts the calls.
 *
 * ⚠ With no provider the value is `undefined` and figures render BARE — which
 * is exactly the pre-TBD-503 output, never a wrong symbol. That is what keeps
 * component tests that render a widget in isolation passing unchanged, and it
 * is deliberate rather than incidental.
 *
 * ⚠ When `Organization.primary_currency` lands (TBD-325 PR 2) the provider is
 * the ONE place that changes. Every consumer reads the hook, so none moves.
 */
import { createContext, useCallback, useContext, useMemo } from "react";

import { deriveOrgCurrency } from "@/lib/currencies";
import { formatMoney } from "@/lib/format";
import { useAccounts } from "@/lib/hooks/use-accounts";

const OrgCurrencyContext = createContext<string | undefined>(undefined);

/**
 * Supplies the org currency to the tree. Mounted once, in the app shell,
 * where accounts are already being fetched.
 *
 * ⚠ Derives from accounts, which is sound only because TBD-325 closed the
 * currency door: an org holds exactly one currency, so reading it off any
 * account is well defined. A MIXED-currency org yields `undefined` and every
 * figure degrades to a bare number, because labelling figures that aggregate
 * differently-denominated accounts with one currency's symbol would be a wrong
 * number wearing a confident label.
 */
export function OrgCurrencyProvider({
  children,
  enabled = true,
}: {
  children: React.ReactNode;
  enabled?: boolean;
}) {
  const { data } = useAccounts(enabled);
  const currency = useMemo(() => deriveOrgCurrency(data), [data]);
  return (
    <OrgCurrencyContext.Provider value={currency}>
      {children}
    </OrgCurrencyContext.Provider>
  );
}

export function useOrgCurrency(): string | undefined {
  // ⚠ CONTEXT ONLY. This hook must never fetch.
  //
  // A version that fell back to `useAccounts()` when no provider was mounted
  // looked convenient and broke a deliberate fence:
  // `accounts-swr-auth-gate.test.tsx` asserts accounts are NOT fetched while
  // auth is still loading, and a leaf calling the hook fired that request
  // regardless. It also made an unrelated modal issue a second request.
  // Formatting a number must not cause IO.
  return useContext(OrgCurrencyContext);
}

/**
 * A bound money formatter for component render paths: `money(1234.56)` gives
 * `"€1,234.56"`.
 *
 * ⚠ Use `formatMoney(value, currency)` directly outside components, and
 * wherever the figure carries its OWN currency rather than the org's —
 * `AccountMonthEndForecast` and `BalancesByTypeTile` render one row per
 * currency, so binding them to the single org currency would render unlike
 * currencies identically.
 */
export function useMoney(): (value: number | string) => string {
  const currency = useOrgCurrency();
  return useCallback(
    (value: number | string) => formatMoney(value, currency),
    [currency],
  );
}
