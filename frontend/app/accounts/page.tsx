"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import HelpTooltip from "@/components/help/HelpTooltip";
import Tooltip from "@/components/Tooltip";
import Spinner from "@/components/ui/Spinner";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import { fetchAll } from "@/lib/pagination";
import { formatAmount } from "@/lib/format";
import {
  useTableState,
  paginate,
  pageCount,
  type SortDir,
} from "@/lib/hooks/use-table-state";
import { SORT_KEY_ACCOUNTS } from "@/lib/hooks/persisted-keys";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import type { Account, AccountType, Transaction } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import OverflowMenu, { type OverflowMenuItem } from "@/components/ui/OverflowMenu";
import AdjustBalanceModal from "@/components/accounts/AdjustBalanceModal";

// Sortable column identifiers for the accounts list.
type AccountSortField = "name" | "type" | "balance";

const ALLOWED_ACCOUNT_SORT_FIELDS: readonly AccountSortField[] = [
  "name",
  "type",
  "balance",
];

// Case-insensitive string compare; null/empty always sort last regardless of
// direction. `factor` (+1 asc, -1 desc) applies only to the value comparison
// so the empty-last sentinel is never flipped by descending direction.
function cmpString(
  a: string | null | undefined,
  b: string | null | undefined,
  factor: 1 | -1,
): number {
  const aEmpty = a == null || a === "";
  const bEmpty = b == null || b === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;  // empty always after non-empty, direction-independent
  if (bEmpty) return -1;
  return factor * a!.localeCompare(b!, undefined, { sensitivity: "base" });
}

function sortAccounts(
  rows: Account[],
  field: AccountSortField,
  dir: SortDir,
): Account[] {
  const factor: 1 | -1 = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    switch (field) {
      case "name":
        return cmpString(a.name, b.name, factor);
      case "type":
        return cmpString(a.account_type_name, b.account_type_name, factor);
      case "balance":
        // balance is typed number but the API/fixtures may serialize it as a
        // decimal string; coerce so the compare is always numeric.
        return factor * (Number(a.balance) - Number(b.balance));
      default:
        return 0;
    }
  });
}

export default function AccountsPage() {
  const { user, loading } = useAuth();
  const [accountTypes, setAccountTypes] = useState<AccountType[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  // All-time pending transactions for the per-account "Pending: €X.XX"
  // row. Pending is a status, not a period concept; a CC charge sitting
  // in pending must be visible whether it was made this month or last.
  const [pendingTransactions, setPendingTransactions] = useState<Transaction[]>([]);
  const [fetching, setFetching] = useState(true);

  const [typeName, setTypeName] = useState("");
  const [editingTypeId, setEditingTypeId] = useState<number | null>(null);
  const [editingTypeName, setEditingTypeName] = useState("");

  // Account edit
  const [editAcctId, setEditAcctId] = useState<number | null>(null);
  const [editAcctName, setEditAcctName] = useState("");
  const [editAcctCloseDay, setEditAcctCloseDay] = useState("");
  // Edit Account Type spec § 5.1 — selected type id during inline edit.
  // The select drives both the close-day input's visibility (§ 5.2) and
  // the type-change confirm modal (§ 5.3).
  const [editAcctTypeId, setEditAcctTypeId] = useState<number | "">("");
  // L3.2 Wave 2A — opening balance fields are editable from the row.
  const [editAcctOpeningBalance, setEditAcctOpeningBalance] = useState("0.00");
  const [editAcctOpeningBalanceDate, setEditAcctOpeningBalanceDate] = useState("");
  // Confirm modal state for type change (spec § 5.3). Holds the
  // pre-resolved old/new type labels + the change-effect copy so the
  // modal message can be a plain string (ConfirmModal does not take
  // rich/JSX content).
  const [pendingTypeChange, setPendingTypeChange] = useState<{
    accountName: string;
    oldTypeLabel: string;
    newTypeLabel: string;
    enteringCC: boolean;
    leavingCC: boolean;
  } | null>(null);

  const [showAccountForm, setShowAccountForm] = useState(false);
  const [acctName, setAcctName] = useState("");
  const [acctTypeId, setAcctTypeId] = useState<number | "">("");
  const [acctCurrency, setAcctCurrency] = useState("EUR");
  const [acctCloseDay, setAcctCloseDay] = useState("");
  // L3.2 Wave 2A — opening balance + date on the create form. The date
  // input defaults to today so most users skip the picker; the contract
  // (§4.4) backfills 0 for existing accounts, so the create form is the
  // first chance to state a real starting amount.
  const todayIso = new Date().toISOString().slice(0, 10);
  const [acctOpeningBalance, setAcctOpeningBalance] = useState("0.00");
  const [acctOpeningBalanceDate, setAcctOpeningBalanceDate] = useState(todayIso);
  const selectedType = accountTypes.find((t) => t.id === acctTypeId) ?? null;

  const [error, setError] = useState("");
  // Non-blocking refresh-error state for the AppShell post-write event
  // listener. The page keeps the previous list; banner offers a Retry.
  const [refreshError, setRefreshError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [confirmDeleteTypeId, setConfirmDeleteTypeId] = useState<number | null>(null);
  const [confirmDeleteAcctId, setConfirmDeleteAcctId] = useState<number | null>(null);
  // Track E: account being adjusted (or null when the modal is closed).
  // Only rendered when the user is admin AND the org has the
  // allow_manual_balance_adjustment flag on.
  const [adjustingAccount, setAdjustingAccount] = useState<Account | null>(null);

  const canAdjustBalance = !!user && isAdmin(user) && user.allow_manual_balance_adjustment;

  const reload = useCallback(async () => {
    // Primary fetches: account types + accounts. A failure here is a
    // real failure — surface it through the existing reload().catch.
    // Run in parallel with the supplementary pending fetch but don't
    // let pending's failure bring the whole page down.
    const pendingPromise = fetchAll<Transaction>("/api/v1/transactions?status=pending")
      // Best-effort: a pending fetch failure must not (a) blank the
      // accounts list on initial load, or (b) make a successful
      // mutation look failed because reload() rejected only due to
      // the pending augment. Resolve with `null` to signal "skip the
      // setState" without rejecting the parent Promise.all.
      .catch(() => null);
    const [types, accts, pending] = await Promise.all([
      apiFetch<AccountType[]>("/api/v1/account-types"),
      apiFetch<Account[]>("/api/v1/accounts"),
      pendingPromise,
    ]);
    setAccountTypes(types ?? []);
    setAccounts(accts ?? []);
    if (pending !== null) setPendingTransactions(pending);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  // After a write from the AppShell-level "+ New Transaction" CTA the
  // accounts page must refresh balances and pending totals (a new
  // expense/income mutates the relevant account's balance and may add
  // a new pending row). reload() is a single composite call; a plain
  // try/catch is enough to drive the inline retry banner.
  const refreshAfterTransactionAdded = useCallback(async () => {
    if (loading || !user) return;
    setRefreshing(true);
    try {
      await reload();
      setRefreshError(false);
    } catch {
      setRefreshError(true);
    } finally {
      setRefreshing(false);
    }
  }, [loading, user, reload]);

  useTransactionAddedListener(() => {
    void refreshAfterTransactionAdded();
  });

  async function handleAddType(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/account-types", { method: "POST", body: JSON.stringify({ name: typeName }) });
      setTypeName("");
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleUpdateType(id: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/account-types/${id}`, { method: "PUT", body: JSON.stringify({ name: editingTypeName }) });
      setEditingTypeId(null);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDeleteType(id: number) {
    setConfirmDeleteTypeId(null);
    setError("");
    try {
      await apiFetch(`/api/v1/account-types/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleAddAccount(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/accounts", {
        method: "POST",
        body: JSON.stringify({
          name: acctName, account_type_id: acctTypeId,
          currency: acctCurrency,
          close_day: selectedType?.slug === "credit_card" && acctCloseDay ? Number(acctCloseDay) : null,
          opening_balance: acctOpeningBalance || "0.00",
          opening_balance_date: acctOpeningBalanceDate || null,
        }),
      });
      setAcctName(""); setAcctTypeId(""); setAcctCloseDay("");
      setAcctOpeningBalance("0.00"); setAcctOpeningBalanceDate(todayIso);
      setShowAccountForm(false);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDeleteAccount(id: number) {
    setConfirmDeleteAcctId(null);
    setError("");
    try {
      await apiFetch(`/api/v1/accounts/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  function startEditAcct(a: Account) {
    setEditAcctId(a.id);
    setEditAcctName(a.name);
    setEditAcctTypeId(a.account_type_id);
    setEditAcctCloseDay(a.close_day ? String(a.close_day) : "");
    setEditAcctOpeningBalance(String(a.opening_balance ?? "0.00"));
    setEditAcctOpeningBalanceDate(a.opening_balance_date ?? "");
  }

  // Resolve the currently-selected edit type so render gates (close-day
  // input visibility, dialog content) can read its slug live. Edit
  // Account Type spec § 5.2.
  const editingAcct = accounts.find((a) => a.id === editAcctId) ?? null;
  const editingTypeSlug =
    accountTypes.find((t) => t.id === editAcctTypeId)?.slug ?? null;

  // Common PUT body builder for the save action. Pulled out so the
  // confirm-modal "Change type" handler can re-use it without
  // duplicating the JSON shape.
  async function _doSaveAcct() {
    if (!editAcctId) return;
    const isCC = editingTypeSlug === "credit_card";
    const body: Record<string, unknown> = {
      name: editAcctName,
      opening_balance: editAcctOpeningBalance || "0.00",
      opening_balance_date: editAcctOpeningBalanceDate || null,
    };
    // Spec § 3.1 — only send close_day when the selected type is CC.
    // The server forces close_day=null on non-CC types regardless of
    // payload, but sending a non-null close_day on a non-CC type yields
    // 400 per the create+update parity rules. So we suppress it
    // entirely when the user is not on CC.
    if (isCC) {
      body.close_day = editAcctCloseDay ? Number(editAcctCloseDay) : null;
    }
    // Always send account_type_id so the cascade and audit logic on
    // the backend trigger. The handler is idempotent when the value
    // equals the current type (no audit row emitted, per § 6).
    if (editAcctTypeId !== "") {
      body.account_type_id = editAcctTypeId;
    }
    await apiFetch(`/api/v1/accounts/${editAcctId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    setEditAcctId(null);
    setEditAcctTypeId("");
    setPendingTypeChange(null);
    await reload();
  }

  async function handleSaveAcct() {
    if (!editAcctId) return;
    setError("");
    // Spec § 5.3 — show the confirm modal ONLY when the type actually
    // changes. Plain name / close-day / opening-balance edits commit
    // straight through.
    if (editingAcct && editAcctTypeId !== "" && editAcctTypeId !== editingAcct.account_type_id) {
      const oldType = accountTypes.find((t) => t.id === editingAcct.account_type_id) ?? null;
      const newType = accountTypes.find((t) => t.id === editAcctTypeId) ?? null;
      setPendingTypeChange({
        accountName: editingAcct.name,
        oldTypeLabel: oldType?.name ?? "current type",
        newTypeLabel: newType?.name ?? "new type",
        leavingCC: oldType?.slug === "credit_card",
        enteringCC: newType?.slug === "credit_card",
      });
      return;
    }
    try {
      await _doSaveAcct();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function confirmTypeChange() {
    setError("");
    try {
      await _doSaveAcct();
    } catch (err) {
      setPendingTypeChange(null);
      setError(extractErrorMessage(err));
    }
  }

  // Compose the confirm-modal message at call time per spec § 5.3 (the
  // shared ConfirmModal takes a plain string, not rich JSX).
  function _typeChangeMessage(p: NonNullable<typeof pendingTypeChange>): string {
    const parts: string[] = [
      `You are changing ${p.accountName} from ${p.oldTypeLabel} to ${p.newTypeLabel}.`,
    ];
    if (p.leavingCC) {
      parts.push("This will clear the closing day on this account.");
    }
    if (p.enteringCC) {
      parts.push(
        "You will need to set a closing day. New transactions on this account will default to Pending until they settle.",
      );
    }
    parts.push("Existing transactions on this account will not change.");
    return parts.join(" ");
  }

  async function handleToggleActive(account: Account) {
    try {
      await apiFetch(`/api/v1/accounts/${account.id}`, { method: "PUT", body: JSON.stringify({ is_active: !account.is_active }) });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleSetDefault(account: Account) {
    try {
      await apiFetch(`/api/v1/accounts/${account.id}`, { method: "PUT", body: JSON.stringify({ is_default: true }) });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  // Per-account pending totals. Income contributes positively, expense
  // negatively (so for a CC, pending is normally negative — money owed).
  // The display below renders Math.abs() and the "Pending:" label, so
  // sign is just used to compute the magnitude correctly.
  const pendingByAccount = pendingTransactions.reduce<Record<number, number>>((acc, tx) => {
    const sign = tx.type === "income" ? 1 : -1;
    acc[tx.account_id] = (acc[tx.account_id] || 0) + Number(tx.amount) * sign;
    return acc;
  }, {});

  // Sort + pagination state, persisted under the existing accounts key.
  // Default sort is name ascending. A differing stored shape (e.g. from the
  // old usePersistedSort schema) simply falls back to defaults — this is a
  // pre-launch app with no back-compat requirement.
  const { sortField, sortDir, setSort, page, setPage, pageSize, setPageSize } =
    useTableState<AccountSortField>({
      key: SORT_KEY_ACCOUNTS,
      defaultSortField: "name",
      defaultSortDir: "asc",
      allowedSortFields: ALLOWED_ACCOUNT_SORT_FIELDS,
    });

  const sortedAccounts = useMemo(
    () => sortAccounts(accounts, sortField, sortDir),
    [accounts, sortField, sortDir],
  );
  const totalAccountPages = pageCount(sortedAccounts.length, pageSize);
  const safePage = Math.min(page, totalAccountPages);
  const pagedAccounts = useMemo(
    () => paginate(sortedAccounts, safePage, pageSize),
    [sortedAccounts, safePage, pageSize],
  );
  const showPagination = totalAccountPages > 1;

  // Click a header: toggle direction if it is already the active column,
  // else switch to that column starting ascending.
  const handleSort = useCallback(
    (field: string) => {
      const f = field as AccountSortField;
      if (f === sortField) {
        setSort(f, sortDir === "asc" ? "desc" : "asc");
      } else {
        setSort(f, "asc");
      }
    },
    [sortField, sortDir, setSort],
  );

  // Shared grid template for the accounts list. The header <tr> and each
  // row <article> MUST use the IDENTICAL template string so the columns
  // line up. The trailing action column is a small FIXED width sized for
  // the inline buttons (Edit, optional Adjust balance) plus the "..."
  // overflow trigger. Without Adjust balance only Edit + "..." sit there
  // (~5rem); with it the column needs room for "Adjust balance" too
  // (~11rem). The first column stays minmax(0,1fr) so the account name
  // takes all the freed space.
  const accountsGridTemplate = canAdjustBalance
    ? "md:grid-cols-[minmax(0,1fr)_8rem_8rem_12rem]"
    : "md:grid-cols-[minmax(0,1fr)_8rem_8rem_5rem]";

  return (
    <AppShell>
      <div
        className="mb-8 flex items-start gap-1"
        data-tour-id="accounts.title"
      >
        <h1 className={`${pageTitle} mb-0`}>Accounts</h1>
        <HelpAnchor section="accounts" label="Accounts" />
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {refreshError && (
        <div
          className={`mb-6 flex items-center justify-between gap-3 ${errorCls}`}
          role="status"
          data-testid="accounts-refresh-error"
        >
          <span>Failed to refresh after the last update. Try again.</span>
          <button
            type="button"
            onClick={() => {
              setRefreshError(false);
              void refreshAfterTransactionAdded();
            }}
            disabled={refreshing}
            className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            {refreshing ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        // Layout: stacks vertically on mobile/tablet (default flex-col),
        // splits into a 1/3 + 2/3 grid at lg+ so the short Account Types
        // table no longer leaves a wide whitespace band above the
        // Accounts list. Items align to start so the Types card keeps its
        // intrinsic height instead of stretching to match Accounts.
        <div
          data-testid="accounts-page-grid"
          className="flex flex-col gap-6 lg:grid lg:grid-cols-3 lg:items-start lg:gap-6"
        >
          {/* Account Types */}
          <div className={`${card} lg:col-span-1`}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Account Types</h2>
            </div>
            <div className="p-6">
              <form onSubmit={handleAddType} className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-center">
                <div className="w-full sm:flex-1">
                  <label htmlFor="type-name" className="sr-only">New type name</label>
                  <input id="type-name" type="text" required placeholder="New type name" value={typeName} onChange={(e) => setTypeName(e.target.value)} className={input} />
                </div>
                <button type="submit" className={`w-full sm:w-auto sm:min-h-0 ${btnPrimary}`}>Add</button>
              </form>
              {/* Column header — visible only on sm+ where the row uses
                  the same grid template. Keeps the type name column
                  proportional and pins the system badge + count to
                  fixed-width slots so longer names can't push them out
                  of alignment. */}
              {accountTypes.length > 0 && (
                <div className="hidden border-b border-border-subtle px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted sm:grid sm:grid-cols-[minmax(0,1fr)_4rem_3rem_auto] sm:items-center sm:gap-3">
                  <span>Type</span>
                  <span className="text-center">Tag</span>
                  <span className="text-right" title="Number of accounts using this type">Count</span>
                  <span className="sr-only">Actions</span>
                </div>
              )}
              <div className="space-y-1">
                {accountTypes.map((at) => (
                  <div key={at.id} className="group flex flex-col gap-2 rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised sm:grid sm:grid-cols-[minmax(0,1fr)_4rem_3rem_auto] sm:items-center sm:gap-3">
                    {editingTypeId === at.id ? (
                      <div className="flex flex-col gap-2 sm:col-span-4 sm:flex-row sm:items-center">
                        <label htmlFor={`edit-type-${at.id}`} className="sr-only">Edit type name</label>
                        <input id={`edit-type-${at.id}`} type="text" value={editingTypeName} onChange={(e) => setEditingTypeName(e.target.value)} className={`w-full sm:flex-1 ${input}`} autoFocus
                          onKeyDown={(e) => { if (e.key === "Enter") handleUpdateType(at.id); if (e.key === "Escape") setEditingTypeId(null); }} />
                        <div className="flex flex-wrap gap-2">
                          <button onClick={() => handleUpdateType(at.id)} className="min-h-[44px] text-sm text-accent hover:text-accent-hover sm:min-h-0">Save</button>
                          <button onClick={() => setEditingTypeId(null)} className="min-h-[44px] text-sm text-text-muted hover:text-text-secondary sm:min-h-0">Cancel</button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <span className="min-w-0 truncate text-sm text-text-primary">{at.name}</span>
                        <span className="text-left sm:text-center">
                          {at.is_system && (
                            <span className="inline-block rounded bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-text-muted">system</span>
                          )}
                        </span>
                        <span className="text-xs tabular-nums text-text-muted sm:text-right" title={`${at.account_count} account(s)`}>{at.account_count}</span>
                        <div className="flex flex-wrap gap-3 sm:justify-end">
                          {!at.is_system && (
                            <>
                              <button onClick={() => { setEditingTypeId(at.id); setEditingTypeName(at.name); }} aria-label={`Edit ${at.name}`} className="min-h-[44px] text-xs text-text-muted hover:text-accent sm:min-h-0">Edit</button>
                              <button onClick={() => setConfirmDeleteTypeId(at.id)} aria-label={`Delete ${at.name}`} className="min-h-[44px] text-xs text-text-muted hover:text-danger sm:min-h-0">Delete</button>
                            </>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                ))}
                {accountTypes.length === 0 && <p className="py-4 text-center text-sm text-text-muted">No account types yet. Add one above.</p>}
              </div>
            </div>
          </div>

          {/* Accounts */}
          <div className={`${card} lg:col-span-2`}>
            <div className={`flex items-center justify-between ${cardHeader}`}>
              <h2 className={cardTitle}>Accounts</h2>
              {accountTypes.length > 0 && (
                <button onClick={() => setShowAccountForm(!showAccountForm)} className="text-xs text-accent hover:text-accent-hover">
                  {showAccountForm ? "Cancel" : "+ Add Account"}
                </button>
              )}
            </div>
            <div className="p-6">
              {showAccountForm && (
                <form onSubmit={handleAddAccount} className="mb-5 space-y-3">
                  <div>
                    <label htmlFor="acct-name" className={label}>Account name</label>
                    <input id="acct-name" type="text" required value={acctName} onChange={(e) => setAcctName(e.target.value)} className={input} />
                  </div>
                  <div>
                    <label htmlFor="acct-type" className={label}>Type</label>
                    <select id="acct-type" required value={acctTypeId} onChange={(e) => setAcctTypeId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                      <option value="">Select type</option>
                      {accountTypes.map((at) => <option key={at.id} value={at.id}>{at.name}</option>)}
                    </select>
                  </div>
                  {selectedType?.slug === "credit_card" && (
                    <div>
                      <label htmlFor="acct-close" className={label}>Bill close day (1-28)</label>
                      {/* Spec § 5.6 — required when the selected type
                          is credit_card. Server-side validation per
                          § 3.1.1 remains the source of truth; this is
                          a UX hint, not a security boundary. */}
                      <input id="acct-close" type="number" required min={1} max={28} value={acctCloseDay} onChange={(e) => setAcctCloseDay(e.target.value)} className={`w-24 ${input}`} placeholder="15" />
                    </div>
                  )}
                  {/* L3.2 Wave 2A — opening balance + date. Optional;
                      defaults are 0 / today. Helper text aimed at the
                      pre-launch friends-only audience: most users won't
                      know their starting balance and that's fine, we
                      simply count from 0. */}
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                    <div className="w-full sm:flex-1">
                      <span className="mb-1.5 flex items-center gap-1">
                        <label htmlFor="acct-opening-balance" className={`${label} mb-0`}>Opening balance</label>
                        <HelpTooltip k="account.opening-balance" />
                      </span>
                      <input
                        id="acct-opening-balance"
                        type="number"
                        step="0.01"
                        value={acctOpeningBalance}
                        onChange={(e) => setAcctOpeningBalance(e.target.value)}
                        className={input}
                      />
                    </div>
                    <div className="w-full sm:w-20">
                      <label htmlFor="acct-currency" className={label}>Currency</label>
                      <input id="acct-currency" type="text" maxLength={3} value={acctCurrency} onChange={(e) => setAcctCurrency(e.target.value.toUpperCase())} className={`sm:text-center ${input}`} />
                    </div>
                    <div className="w-full sm:w-44">
                      <label htmlFor="acct-opening-balance-date" className={label}>Starting from</label>
                      <input
                        id="acct-opening-balance-date"
                        type="date"
                        value={acctOpeningBalanceDate}
                        onChange={(e) => setAcctOpeningBalanceDate(e.target.value)}
                        className={input}
                      />
                    </div>
                  </div>
                  <p className="text-xs text-text-muted">
                    Your account&apos;s starting amount. Leave at 0 if you don&apos;t know.
                  </p>
                  <button type="submit" className={`w-full sm:w-auto sm:min-h-0 ${btnPrimary}`}>Create Account</button>
                </form>
              )}
              {/* Sortable column header — visible only on md+ where the
                  row uses the same outer grid template. The header is a
                  one-row <table> so it can host the shared SortableHeader
                  <th> cells (name, type, balance) with proper aria-sort,
                  while the rows below stay as the responsive grid articles
                  that the layout tests rely on. A CSS grid on the <tr>
                  pins each header cell over its matching row column. The
                  Actions header is sr-only since that column is button
                  links rather than tabular data. */}
              {accounts.length > 0 && (
                <table
                  data-testid="accounts-list-header"
                  className="hidden w-full border-b border-border-subtle md:table"
                >
                  <thead>
                    <tr className={`grid ${accountsGridTemplate} items-center gap-4 px-3`}>
                      <SortableHeader
                        label="Account"
                        field="name"
                        activeField={sortField}
                        dir={sortDir}
                        onSort={handleSort}
                      />
                      <SortableHeader
                        label="Type"
                        field="type"
                        activeField={sortField}
                        dir={sortDir}
                        onSort={handleSort}
                      />
                      <SortableHeader
                        label="Balance"
                        field="balance"
                        activeField={sortField}
                        dir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                      <th className="px-3 py-2 text-right">
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                </table>
              )}
              <div className="space-y-1">
                {pagedAccounts.map((a) => editAcctId === a.id ? (
                  <div key={a.id} className="flex flex-col gap-3 rounded-md bg-surface-raised px-3 py-3">
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                      <input aria-label="Account name" type="text" value={editAcctName} onChange={(e) => setEditAcctName(e.target.value)} className={`w-full text-sm sm:flex-1 ${input}`}
                        onKeyDown={(e) => { if (e.key === "Enter") handleSaveAcct(); if (e.key === "Escape") { setEditAcctId(null); setEditAcctTypeId(""); } }} autoFocus />
                      {/* Edit Account Type spec § 5.1 — type select.
                          Drives close-day input visibility below via
                          editingTypeSlug. */}
                      <select
                        aria-label="Account type"
                        value={editAcctTypeId}
                        onChange={(e) => {
                          const next = e.target.value === "" ? "" : Number(e.target.value);
                          setEditAcctTypeId(next);
                          // Spec § 5.2 — clear close-day local state
                          // when leaving CC, so a stale value can't be
                          // sent.
                          const nextSlug = accountTypes.find((t) => t.id === next)?.slug ?? null;
                          if (nextSlug !== "credit_card") setEditAcctCloseDay("");
                        }}
                        className={`w-full text-sm sm:w-44 ${input}`}
                      >
                        {accountTypes.map((at) => (
                          <option key={at.id} value={at.id}>{at.name}</option>
                        ))}
                      </select>
                      {/* Spec § 5.2 — close-day input visibility is
                          driven by the SELECTED type, not the row's
                          current type. The moment the user picks Credit
                          Card the input appears; the moment they pick
                          anything else it disappears. */}
                      {editingTypeSlug === "credit_card" && (
                        <input aria-label="Close day" type="number" min={1} max={28} value={editAcctCloseDay} onChange={(e) => setEditAcctCloseDay(e.target.value)} placeholder="Close day" className={`w-full text-sm sm:w-24 ${input}`} />
                      )}
                    </div>
                    {/* L3.2 Wave 2A — opening balance edit row. Two
                        compact fields, audit-logged on the backend. */}
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:gap-3">
                      <div className="w-full sm:flex-1">
                        <label htmlFor={`edit-acct-opening-balance-${a.id}`} className={label}>Opening balance</label>
                        <input
                          id={`edit-acct-opening-balance-${a.id}`}
                          type="number"
                          step="0.01"
                          value={editAcctOpeningBalance}
                          onChange={(e) => setEditAcctOpeningBalance(e.target.value)}
                          className={input}
                        />
                      </div>
                      <div className="w-full sm:w-44">
                        <label htmlFor={`edit-acct-opening-balance-date-${a.id}`} className={label}>Starting from</label>
                        <input
                          id={`edit-acct-opening-balance-date-${a.id}`}
                          type="date"
                          value={editAcctOpeningBalanceDate}
                          onChange={(e) => setEditAcctOpeningBalanceDate(e.target.value)}
                          className={input}
                        />
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button onClick={handleSaveAcct} className="min-h-[44px] text-xs text-accent hover:text-accent-hover sm:min-h-0">Save</button>
                      <button onClick={() => { setEditAcctId(null); setEditAcctTypeId(""); }} className="min-h-[44px] text-xs text-text-muted sm:min-h-0">Cancel</button>
                    </div>
                  </div>
                ) : (
                  <article
                    key={a.id}
                    data-testid={`account-row-${a.id}`}
                    data-account-name={a.name}
                    className={`flex flex-col gap-3 rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised md:grid ${accountsGridTemplate} md:items-center md:gap-4 ${!a.is_active ? "opacity-40" : ""}`}
                  >
                    {/* Description column: name + meta. The "DEFAULT"
                        badge is a fixed-width inline pill (NOT trailing
                        "· default" text), so toggling default never
                        changes how much room neighbouring text gets. The
                        account type moved to its own sortable column at
                        md+; on mobile it is repeated inline here so the
                        stacked card still reads "name · type". */}
                    <div className="min-w-0 flex-1 md:flex-none">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span className="truncate text-sm font-medium text-text-primary">{a.name}</span>
                        {a.is_default && (
                          <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-text-secondary">
                            Default
                          </span>
                        )}
                        <span className="text-xs text-text-muted md:hidden">{a.account_type_name}</span>
                        {a.close_day && <span className="text-xs text-text-muted">· closes day {a.close_day}</span>}
                        {!a.is_active && <span className="text-xs text-danger">inactive</span>}
                      </div>
                    </div>
                    {/* Type column — visible at md+ as its own sortable
                        column. Hidden on mobile where it is shown inline
                        next to the name above. */}
                    <div className="hidden min-w-0 md:block">
                      <span className="truncate text-xs text-text-muted">{a.account_type_name}</span>
                    </div>
                    {/* Fixed-width balance column — the outer grid
                        reserves an 8rem slot at md:, so toggling
                        Default never shifts the numbers. tabular-nums
                        + text-right keep digits aligned across rows. */}
                    <div className="flex shrink-0 flex-col items-start gap-0.5 md:items-end">
                      <span className="text-sm tabular-nums text-text-primary">
                        {formatAmount(a.balance)}{" "}
                        <span className="text-text-muted">{a.currency}</span>
                      </span>
                      {pendingByAccount[a.id] ? (
                        <span className="inline-flex items-center gap-1 text-xs tabular-nums text-text-muted">
                          <span>Pending: {formatAmount(Math.abs(pendingByAccount[a.id]))}</span>
                          <Tooltip
                            content="Sum of transactions still marked Pending on this account. They do not move the balance yet, but they shape the end of month forecast."
                            learnMoreSection="accounts"
                            triggerLabel="What does Pending mean for this account?"
                          />
                        </span>
                      ) : null}
                      {/* L3.2 Wave 2A — opening balance hint. Only
                          surface when the user set a non-zero value;
                          accounts left at the 0 backfill stay quiet so
                          the column doesn't fill with "Opening: 0.00"
                          noise. */}
                      {Number(a.opening_balance) !== 0 ? (
                        <span className="text-xs tabular-nums text-text-muted">
                          Opening: {formatAmount(Number(a.opening_balance))}
                          {a.opening_balance_date ? ` since ${a.opening_balance_date}` : ""}
                        </span>
                      ) : null}
                    </div>
                    {/* Action column. Edit (and Adjust balance, when the
                        admin permission is on AND the account is active)
                        stay inline; the rarer actions move into a per-row
                        "..." overflow menu so the action column can be a
                        small fixed width and the Account name column gets
                        the freed space. The fixed width is shared with the
                        header via accountsGridTemplate so the columns stay
                        aligned. */}
                    {(() => {
                      const overflowItems: OverflowMenuItem[] = [];
                      if (!a.is_default && a.is_active) {
                        overflowItems.push({
                          label: "Set default",
                          ariaLabel: `Set ${a.name} as default`,
                          onSelect: () => { void handleSetDefault(a); },
                        });
                      }
                      overflowItems.push({
                        label: a.is_active ? "Deactivate" : "Activate",
                        ariaLabel: a.is_active ? `Deactivate ${a.name}` : `Activate ${a.name}`,
                        onSelect: () => { void handleToggleActive(a); },
                      });
                      overflowItems.push({
                        label: "Delete",
                        ariaLabel: `Delete ${a.name}`,
                        danger: true,
                        onSelect: () => setConfirmDeleteAcctId(a.id),
                      });
                      return (
                        <div
                          data-testid={`account-row-actions-${a.id}`}
                          className="flex items-center justify-end gap-3"
                        >
                          <button onClick={() => startEditAcct(a)} aria-label={`Edit ${a.name}`} className="min-h-[44px] whitespace-nowrap text-xs text-text-muted hover:text-accent md:min-h-0">Edit</button>
                          {canAdjustBalance && a.is_active && (
                            <button
                              onClick={() => setAdjustingAccount(a)}
                              aria-label={`Adjust balance of ${a.name}`}
                              className="min-h-[44px] whitespace-nowrap text-xs text-text-muted hover:text-accent md:min-h-0"
                            >
                              Adjust balance
                            </button>
                          )}
                          <OverflowMenu
                            items={overflowItems}
                            label={`More actions for ${a.name}`}
                            testId={`account-row-overflow-${a.id}`}
                          />
                        </div>
                      );
                    })()}
                  </article>
                ))}
                {accounts.length === 0 && (
                  <p className="py-4 text-center text-sm text-text-muted">
                    {accountTypes.length === 0 ? "Create an account type first." : "No accounts yet. Click '+ Add Account' above."}
                  </p>
                )}
              </div>
              {showPagination && (
                <div className="mt-2 border-t border-border-subtle">
                  <Pagination
                    page={safePage}
                    pageSize={pageSize}
                    total={accounts.length}
                    onPageChange={setPage}
                    onPageSizeChange={setPageSize}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      <ConfirmModal
        open={confirmDeleteTypeId !== null}
        title="Delete Account Type"
        message="Delete this account type?"
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { if (confirmDeleteTypeId !== null) handleDeleteType(confirmDeleteTypeId); }}
        onCancel={() => setConfirmDeleteTypeId(null)}
      />
      <ConfirmModal
        open={confirmDeleteAcctId !== null}
        title="Delete Account"
        message="Delete this account?"
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { if (confirmDeleteAcctId !== null) handleDeleteAccount(confirmDeleteAcctId); }}
        onCancel={() => setConfirmDeleteAcctId(null)}
      />
      {/* Edit Account Type spec § 5.3 — confirm dialog for type
          change. Plain-string message composed at call time. */}
      <ConfirmModal
        open={pendingTypeChange !== null}
        title="Change account type?"
        message={pendingTypeChange ? _typeChangeMessage(pendingTypeChange) : ""}
        confirmLabel="Change type"
        variant="warning"
        onConfirm={confirmTypeChange}
        onCancel={() => setPendingTypeChange(null)}
      />
      {adjustingAccount && (
        <AdjustBalanceModal
          account={adjustingAccount}
          onClose={() => setAdjustingAccount(null)}
          onAdjusted={async () => {
            setAdjustingAccount(null);
            await reload();
          }}
        />
      )}
    </AppShell>
  );
}
