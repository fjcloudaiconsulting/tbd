"use client";

import { useCallback, useEffect, useState } from "react";

import AnchorZone, { AnchorZoneSlot } from "@/components/floating/AnchorZone";
import SlideInPanel from "@/components/floating/SlideInPanel";
import TransactionForm from "@/components/floating/TransactionForm";
import { apiFetch } from "@/lib/api";
import type { Account, Category } from "@/lib/types";

/**
 * Floating "Add Transaction" button. Lives in the bottom-right anchor
 * zone (primary slot — sits at the bottom of the cluster, closest to
 * the thumb on mobile). Click opens a SlideInPanel with the
 * TransactionForm inside.
 *
 * Data loading: this component owns its accounts/categories fetch so
 * pages can mount it without prop-drilling reference data. Refs load
 * on first mount and on every panel open (cheap, keeps the form fresh
 * when the user edits accounts or categories elsewhere).
 *
 * onTransactionAdded: optional callback so the host page can refresh
 * its own data (e.g. dashboard transaction list) when a tx lands. The
 * FAB itself does not refresh anything other than its own ref data.
 */

export interface AddTransactionFabProps {
  /** Pre-select an account (e.g. on /accounts/{id}). */
  defaultAccountId?: number | null;
  /** Pre-select a category (e.g. on a future category-detail page). */
  defaultCategoryId?: number | null;
  /** Called after every successful transaction save. */
  onTransactionAdded?: () => void;
}

export default function AddTransactionFab({
  defaultAccountId = null,
  defaultCategoryId = null,
  onTransactionAdded,
}: AddTransactionFabProps) {
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
      // Swallow load errors silently — the form will render an empty
      // state ("Create at least one account and one category...").
      // Any submit error surfaces inline in the form.
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void loadRefs();
  }, [loadRefs]);

  function handleOpen() {
    // Refresh refs so newly-added accounts/categories show up the next
    // time the user pops the panel without a full reload.
    void loadRefs();
    setOpen(true);
  }

  return (
    <>
      <AnchorZone>
        <AnchorZoneSlot slot="primary">
          <button
            type="button"
            onClick={handleOpen}
            aria-label="Add transaction"
            data-testid="add-transaction-fab"
            className="flex h-14 w-14 items-center justify-center rounded-full bg-accent text-accent-text shadow-lg transition-transform hover:scale-105 hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
          >
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
          </button>
        </AnchorZoneSlot>
      </AnchorZone>

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
            defaultAccountId={defaultAccountId}
            defaultCategoryId={defaultCategoryId}
            onSaved={() => setOpen(false)}
            onCategoryCreated={(cat) => setCategories((prev) => [...prev, cat])}
            onTransactionAdded={onTransactionAdded}
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
