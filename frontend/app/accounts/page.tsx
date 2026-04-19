"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import type { Account, AccountType } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";

export default function AccountsPage() {
  const { user, loading } = useAuth();
  const [accountTypes, setAccountTypes] = useState<AccountType[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [fetching, setFetching] = useState(true);

  const [typeName, setTypeName] = useState("");
  const [editingTypeId, setEditingTypeId] = useState<number | null>(null);
  const [editingTypeName, setEditingTypeName] = useState("");

  // Account edit
  const [editAcctId, setEditAcctId] = useState<number | null>(null);
  const [editAcctName, setEditAcctName] = useState("");
  const [editAcctCloseDay, setEditAcctCloseDay] = useState("");

  const [showAccountForm, setShowAccountForm] = useState(false);
  const [acctName, setAcctName] = useState("");
  const [acctTypeId, setAcctTypeId] = useState<number | "">("");
  const [acctBalance, setAcctBalance] = useState("0.00");
  const [acctCurrency, setAcctCurrency] = useState("EUR");
  const [acctCloseDay, setAcctCloseDay] = useState("");
  const selectedType = accountTypes.find((t) => t.id === acctTypeId) ?? null;

  const [error, setError] = useState("");
  const [confirmDeleteTypeId, setConfirmDeleteTypeId] = useState<number | null>(null);
  const [confirmDeleteAcctId, setConfirmDeleteAcctId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    const [types, accts] = await Promise.all([
      apiFetch<AccountType[]>("/api/v1/account-types"),
      apiFetch<Account[]>("/api/v1/accounts"),
    ]);
    setAccountTypes(types ?? []);
    setAccounts(accts ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

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
          name: acctName, account_type_id: acctTypeId, balance: acctBalance,
          currency: acctCurrency,
          close_day: selectedType?.slug === "credit_card" && acctCloseDay ? Number(acctCloseDay) : null,
        }),
      });
      setAcctName(""); setAcctTypeId(""); setAcctBalance("0.00"); setAcctCloseDay(""); setShowAccountForm(false);
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
    setEditAcctCloseDay(a.close_day ? String(a.close_day) : "");
  }

  async function handleSaveAcct() {
    if (!editAcctId) return;
    setError("");
    try {
      await apiFetch(`/api/v1/accounts/${editAcctId}`, {
        method: "PUT",
        body: JSON.stringify({
          name: editAcctName,
          close_day: editAcctCloseDay ? Number(editAcctCloseDay) : null,
        }),
      });
      setEditAcctId(null);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleToggleActive(account: Account) {
    try {
      await apiFetch(`/api/v1/accounts/${account.id}`, { method: "PUT", body: JSON.stringify({ is_active: !account.is_active }) });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Accounts</h1>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          {/* Account Types */}
          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Account Types</h2>
            </div>
            <div className="p-6">
              <form onSubmit={handleAddType} className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-center">
                <div className="w-full sm:flex-1">
                  <label htmlFor="type-name" className="sr-only">New type name</label>
                  <input id="type-name" type="text" required placeholder="New type name" value={typeName} onChange={(e) => setTypeName(e.target.value)} className={input} />
                </div>
                <button type="submit" className={`w-full min-h-[44px] sm:w-auto sm:min-h-0 ${btnPrimary}`}>Add</button>
              </form>
              <div className="space-y-1">
                {accountTypes.map((at) => (
                  <div key={at.id} className="group flex flex-col gap-2 rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised sm:flex-row sm:items-center sm:justify-between">
                    {editingTypeId === at.id ? (
                      <div className="flex flex-1 flex-col gap-2 sm:flex-row sm:items-center">
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
                        <div className="flex min-w-0 flex-1 items-center gap-2">
                          <span className="truncate text-sm text-text-primary">{at.name}</span>
                          {at.is_system && <span className="shrink-0 rounded bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-text-muted">system</span>}
                          <span className="shrink-0 text-xs text-text-muted" title={`${at.account_count} account(s)`}>{at.account_count}</span>
                        </div>
                        <div className="flex flex-wrap gap-3">
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
          <div className={card}>
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
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                    <div className="w-full sm:flex-1">
                      <label htmlFor="acct-balance" className={label}>Initial balance</label>
                      <input id="acct-balance" type="number" step="0.01" value={acctBalance} onChange={(e) => setAcctBalance(e.target.value)} className={input} />
                    </div>
                    <div className="w-full sm:w-20">
                      <label htmlFor="acct-currency" className={label}>Currency</label>
                      <input id="acct-currency" type="text" maxLength={3} value={acctCurrency} onChange={(e) => setAcctCurrency(e.target.value.toUpperCase())} className={`sm:text-center ${input}`} />
                    </div>
                  </div>
                  {selectedType?.slug === "credit_card" && (
                    <div>
                      <label htmlFor="acct-close" className={label}>Bill close day (1-28)</label>
                      <input id="acct-close" type="number" min={1} max={28} value={acctCloseDay} onChange={(e) => setAcctCloseDay(e.target.value)} className={`w-24 ${input}`} placeholder="15" />
                    </div>
                  )}
                  <button type="submit" className={`w-full min-h-[44px] sm:w-auto sm:min-h-0 ${btnPrimary}`}>Create Account</button>
                </form>
              )}
              <div className="space-y-1">
                {accounts.map((a) => editAcctId === a.id ? (
                  <div key={a.id} className="flex flex-col gap-2 rounded-md bg-surface-raised px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3">
                    <input aria-label="Account name" type="text" value={editAcctName} onChange={(e) => setEditAcctName(e.target.value)} className={`w-full text-sm sm:flex-1 ${input}`}
                      onKeyDown={(e) => { if (e.key === "Enter") handleSaveAcct(); if (e.key === "Escape") setEditAcctId(null); }} autoFocus />
                    {a.account_type_slug === "credit_card" && (
                      <input aria-label="Close day" type="number" min={1} max={28} value={editAcctCloseDay} onChange={(e) => setEditAcctCloseDay(e.target.value)} placeholder="Close day" className={`w-full text-sm sm:w-24 ${input}`} />
                    )}
                    <div className="flex flex-wrap gap-2">
                      <button onClick={handleSaveAcct} className="min-h-[44px] text-xs text-accent hover:text-accent-hover sm:min-h-0">Save</button>
                      <button onClick={() => setEditAcctId(null)} className="min-h-[44px] text-xs text-text-muted sm:min-h-0">Cancel</button>
                    </div>
                  </div>
                ) : (
                  <article key={a.id} className={`flex flex-col gap-3 rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised md:flex-row md:items-center md:justify-between ${!a.is_active ? "opacity-40" : ""}`}>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span className="truncate text-sm font-medium text-text-primary">{a.name}</span>
                        <span className="text-xs text-text-muted">{a.account_type_name}</span>
                        {a.is_default && <span className="text-xs text-accent">· default</span>}
                        {a.close_day && <span className="text-xs text-text-muted">· closes day {a.close_day}</span>}
                        {!a.is_active && <span className="text-xs text-danger">inactive</span>}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-4 md:ml-auto">
                      <span className="text-sm tabular-nums text-text-primary">
                        {formatAmount(a.balance)}{" "}
                        <span className="text-text-muted">{a.currency}</span>
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-3">
                      <button onClick={() => startEditAcct(a)} aria-label={`Edit ${a.name}`} className="min-h-[44px] text-xs text-text-muted hover:text-accent md:min-h-0">Edit</button>
                      {!a.is_default && a.is_active && (
                        <button onClick={async () => { try { await apiFetch(`/api/v1/accounts/${a.id}`, { method: "PUT", body: JSON.stringify({ is_default: true }) }); await reload(); } catch (err) { setError(extractErrorMessage(err)); } }} aria-label={`Set ${a.name} as default`} className="min-h-[44px] text-xs text-text-muted hover:text-accent md:min-h-0">
                          Default
                        </button>
                      )}
                      <button onClick={() => handleToggleActive(a)} aria-label={a.is_active ? `Deactivate ${a.name}` : `Activate ${a.name}`} className="min-h-[44px] text-xs text-text-muted hover:text-text-secondary md:min-h-0">
                        {a.is_active ? "Deactivate" : "Activate"}
                      </button>
                      <button onClick={() => setConfirmDeleteAcctId(a.id)} aria-label={`Delete ${a.name}`} className="min-h-[44px] text-xs text-text-muted hover:text-danger md:min-h-0">Delete</button>
                    </div>
                  </article>
                ))}
                {accounts.length === 0 && (
                  <p className="py-4 text-center text-sm text-text-muted">
                    {accountTypes.length === 0 ? "Create an account type first." : "No accounts yet. Click '+ Add Account' above."}
                  </p>
                )}
              </div>
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
    </AppShell>
  );
}
