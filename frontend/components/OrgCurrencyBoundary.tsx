"use client";

/**
 * Mounts `OrgCurrencyProvider` at the top of the authenticated tree (TBD-503).
 *
 * ⚠⚠ WHY THIS LIVES IN THE ROOT LAYOUT AND NOT IN `AppShell`.
 * A component cannot consume a provider it renders. Most pages here are shaped
 * `function XPage() { const money = useMoney(); ... return <AppShell>…</AppShell> }`
 * — so a provider mounted inside `AppShell` is created *below* the component
 * that formats the money. `useMoney()` in the page body then reads the default
 * context (`undefined`) and renders bare, while child components one level down
 * render prefixed. That produced exactly the split the operator spotted:
 * account tiles showed `€16,062.60` while the transactions, budgets and
 * forecast Amount columns stayed bare.
 *
 * The layout is the only node above every page component, so it is the only
 * altitude that covers both.
 *
 * ⚠ The auth gate lives HERE, not in `OrgCurrencyProvider`. Keeping `useAuth`
 * out of `use-org-currency.tsx` is deliberate: that module is imported by leaf
 * components (modals, tiles, chart tooltips) and `useAuth` throws outside a
 * provider, which previously made every one of them require an AuthProvider
 * and broke 257 tests. This wrapper is the single place the two meet, and it
 * only ever renders inside `AuthProvider`.
 */
import { useAuth } from "@/components/auth/AuthProvider";
import { OrgCurrencyProvider } from "@/lib/hooks/use-org-currency";

export default function OrgCurrencyBoundary({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading } = useAuth();
  // Same gate every other `useAccounts` consumer uses: no fetch before the
  // bearer token is set. `accounts-swr-auth-gate.test.tsx` fences the property.
  return (
    <OrgCurrencyProvider enabled={!loading && !!user}>
      {children}
    </OrgCurrencyProvider>
  );
}
