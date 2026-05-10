"use client";

import { useCallback, useEffect, useState } from "react";

import { Plus } from "lucide-react";

import SlideInPanel from "@/components/floating/SlideInPanel";
import TransactionForm from "@/components/floating/TransactionForm";
import { apiFetch } from "@/lib/api";
import { btnPrimary } from "@/lib/styles";
import type { Account, Category } from "@/lib/types";

/**
 * AppShell-level "+ New Transaction" CTA.
 *
 * Replaces the floating Add Transaction FAB shipped in PR #193. The FAB
 * pattern was the wrong vernacular for an editorial-confident, planful
 * financial planner, the brass-action affordance now sits in the page
 * header alongside the existing chrome (Docs, theme toggle, trial
 * banner). One brass moment per region: this CTA owns it on the core
 * money routes.
 *
 * Visibility: AppShell renders this only on the route allow-list (see
 * AppShell.tsx). No need to gate again here.
 *
 * Data refresh after submit: dispatches a `pfv:transaction-added`
 * window event. Pages that care about the post-write state (Dashboard,
 * Transactions, ...) subscribe and re-fetch their own data. Same
 * idiom as `auth:unauthenticated` in lib/api.ts. Decoupled, no prop
 * drilling, no RSC refresh that would skip client-side useEffect
 * fetches.
 *
 * Responsive label: at narrow viewports the visible label collapses
 * to icon-only, matching the AppShell header's other affordances
 * (Docs, theme toggle) which already collapse to icon-only on mobile.
 * The accessible name stays "New transaction" via aria-label so
 * screen readers and keyboard users keep the same affordance label
 * regardless of viewport.
 */

export default function AppShellAddTransactionCta() {
  const [open, setOpen] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [loaded, setLoaded] = useState(false);

  const loadRefs = useCallback(async () => {
    try {
      const [accts, cats] = await Promise.all([
        apiFetch<Account[]>("/api/v1/accounts"),
        apiFetch<Category[]>("/api/v1/categories"),
      ]);
      setAccounts(accts ?? []);
      setCategories(cats ?? []);
      setLoaded(true);
    } catch {
      // Swallow ref-load errors silently. The form falls through to its
      // empty state ("Create at least one account and one category...")
      // and any submit error surfaces inline. The CTA itself stays
      // clickable so the user can retry.
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void loadRefs();
  }, [loadRefs]);

  function handleOpen() {
    // Refresh refs so newly-added accounts/categories show up next time
    // the user pops the panel without a full page reload.
    void loadRefs();
    setOpen(true);
  }

  function handleTransactionAdded() {
    // Pages subscribe to this on mount and re-fetch their own data. See
    // AppShellAddTransactionCta jsdoc above for rationale.
    if (typeof window !== "undefined") {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={handleOpen}
        aria-label="New transaction"
        data-testid="appshell-add-transaction-cta"
        className={`${btnPrimary} inline-flex min-h-[44px] items-center gap-1.5`}
      >
        <Plus className="h-4 w-4" aria-hidden="true" />
        <span className="hidden sm:inline">New transaction</span>
      </button>

      <SlideInPanel
        open={open}
        onClose={() => setOpen(false)}
        title="Add transaction"
        testId="add-transaction-panel"
      >
        {loaded ? (
          <TransactionForm
            accounts={accounts}
            categories={categories}
            onSaved={() => setOpen(false)}
            onCategoryCreated={(cat) => setCategories((prev) => [...prev, cat])}
            onTransactionAdded={handleTransactionAdded}
          />
        ) : (
          <div className="flex items-center justify-center py-12 text-sm text-text-muted">
            Loading...
          </div>
        )}
      </SlideInPanel>
    </>
  );
}

/**
 * Route allow-list helper. Exposed for AppShell to gate visibility, and
 * for unit tests to assert the predicate without rendering the shell.
 *
 * Show on the core money routes; hide on settings/admin/system. The
 * empty-string fallback ("/") is treated as not-a-money-route, the
 * /login redirect runs before AppShell mounts, so we won't see "/" in
 * practice, but the predicate stays well-defined.
 */
const SHOW_ON: readonly string[] = [
  "/dashboard",
  "/transactions",
  "/accounts",
  "/categories",
  "/forecast-plans",
  "/budgets",
  "/recurring",
];

const HIDE_PREFIXES: readonly string[] = [
  "/settings/",
  "/admin/",
  "/system/",
];

export function shouldShowAddTransactionCta(pathname: string | null): boolean {
  if (!pathname) return false;
  // Hide-list wins over show-list. `/admin` is a money-adjacent root
  // but its children are platform-admin, not user money flows.
  if (HIDE_PREFIXES.some((p) => pathname.startsWith(p))) return false;
  if (pathname === "/settings" || pathname === "/admin" || pathname === "/system") {
    return false;
  }
  return SHOW_ON.some((r) => pathname === r || pathname.startsWith(r + "/"));
}
