"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { Account, AccountType } from "@/lib/types";

export default function AccountsPage() {
  const { user, loading } = useAuth();
  const [accountTypes, setAccountTypes] = useState<AccountType[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);

  const [typeName, setTypeName] = useState("");
  const [editingTypeId, setEditingTypeId] = useState<number | null>(null);
  const [editingTypeName, setEditingTypeName] = useState("");

  const [showAccountForm, setShowAccountForm] = useState(false);
  const [acctName, setAcctName] = useState("");
  const [acctTypeId, setAcctTypeId] = useState<number | "">("");
  const [acctBalance, setAcctBalance] = useState("0.00");
  const [acctCurrency, setAcctCurrency] = useState("EUR");

  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    const [types, accts] = await Promise.all([
      apiFetch<AccountType[]>("/api/v1/account-types"),
      apiFetch<Account[]>("/api/v1/accounts"),
    ]);
    setAccountTypes(types);
    setAccounts(accts);
  }, []);

  useEffect(() => {
    if (!loading && user) {
      reload().catch(() => {});
    }
  }, [loading, user, reload]);

  async function handleAddType(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/account-types", {
        method: "POST",
        body: JSON.stringify({ name: typeName }),
      });
      setTypeName("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleUpdateType(id: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/account-types/${id}`, {
        method: "PUT",
        body: JSON.stringify({ name: editingTypeName }),
      });
      setEditingTypeId(null);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleDeleteType(id: number) {
    if (!confirm("Delete this account type?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/account-types/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleAddAccount(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/accounts", {
        method: "POST",
        body: JSON.stringify({
          name: acctName,
          account_type_id: acctTypeId,
          balance: acctBalance,
          currency: acctCurrency,
        }),
      });
      setAcctName("");
      setAcctTypeId("");
      setAcctBalance("0.00");
      setShowAccountForm(false);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleDeleteAccount(id: number) {
    if (!confirm("Delete this account?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/accounts/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleToggleActive(account: Account) {
    try {
      await apiFetch(`/api/v1/accounts/${account.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_active: !account.is_active }),
      });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  const inputClass =
    "w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none";

  return (
    <AppShell>
      <h1 className="mb-8 font-display text-2xl text-text-primary">Accounts</h1>

      {error && (
        <div className="mb-6 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Account Types */}
        <div className="rounded-lg border border-border bg-surface">
          <div className="border-b border-border px-6 py-4">
            <h2 className="text-xs font-medium uppercase tracking-wider text-text-muted">
              Account Types
            </h2>
          </div>
          <div className="p-6">
            <form onSubmit={handleAddType} className="mb-5 flex gap-2">
              <input
                type="text"
                required
                placeholder="New type name"
                value={typeName}
                onChange={(e) => setTypeName(e.target.value)}
                className={`flex-1 ${inputClass}`}
              />
              <button
                type="submit"
                className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover"
              >
                Add
              </button>
            </form>
            <div className="space-y-1">
              {accountTypes.map((at) => (
                <div
                  key={at.id}
                  className="flex items-center justify-between rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised"
                >
                  {editingTypeId === at.id ? (
                    <div className="flex flex-1 gap-2">
                      <input
                        type="text"
                        value={editingTypeName}
                        onChange={(e) => setEditingTypeName(e.target.value)}
                        className={`flex-1 ${inputClass}`}
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleUpdateType(at.id);
                          if (e.key === "Escape") setEditingTypeId(null);
                        }}
                      />
                      <button
                        onClick={() => handleUpdateType(at.id)}
                        className="text-sm text-accent hover:text-accent-hover"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingTypeId(null)}
                        className="text-sm text-text-muted hover:text-text-secondary"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-text-primary">{at.name}</span>
                        <span className="text-xs text-text-muted">
                          {at.account_count}
                        </span>
                      </div>
                      <div className="flex gap-3 opacity-0 group-hover:opacity-100 [div:hover>&]:opacity-100">
                        <button
                          onClick={() => {
                            setEditingTypeId(at.id);
                            setEditingTypeName(at.name);
                          }}
                          className="text-xs text-text-muted hover:text-accent"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDeleteType(at.id)}
                          className="text-xs text-text-muted hover:text-danger"
                        >
                          Delete
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
              {accountTypes.length === 0 && (
                <p className="py-4 text-center text-sm text-text-muted">
                  No account types yet. Add one above.
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Accounts */}
        <div className="rounded-lg border border-border bg-surface">
          <div className="flex items-center justify-between border-b border-border px-6 py-4">
            <h2 className="text-xs font-medium uppercase tracking-wider text-text-muted">
              Accounts
            </h2>
            {accountTypes.length > 0 && (
              <button
                onClick={() => setShowAccountForm(!showAccountForm)}
                className="text-xs text-accent hover:text-accent-hover"
              >
                {showAccountForm ? "Cancel" : "+ Add Account"}
              </button>
            )}
          </div>
          <div className="p-6">
            {showAccountForm && (
              <form onSubmit={handleAddAccount} className="mb-5 space-y-3">
                <input
                  type="text"
                  required
                  placeholder="Account name"
                  value={acctName}
                  onChange={(e) => setAcctName(e.target.value)}
                  className={inputClass}
                />
                <select
                  required
                  value={acctTypeId}
                  onChange={(e) =>
                    setAcctTypeId(
                      e.target.value === "" ? "" : Number(e.target.value)
                    )
                  }
                  className={inputClass}
                >
                  <option value="">Select type</option>
                  {accountTypes.map((at) => (
                    <option key={at.id} value={at.id}>
                      {at.name}
                    </option>
                  ))}
                </select>
                <div className="flex gap-2">
                  <input
                    type="number"
                    step="0.01"
                    placeholder="Initial balance"
                    value={acctBalance}
                    onChange={(e) => setAcctBalance(e.target.value)}
                    className={`flex-1 ${inputClass}`}
                  />
                  <input
                    type="text"
                    maxLength={3}
                    value={acctCurrency}
                    onChange={(e) =>
                      setAcctCurrency(e.target.value.toUpperCase())
                    }
                    className={`w-16 text-center ${inputClass}`}
                  />
                </div>
                <button
                  type="submit"
                  className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover"
                >
                  Create Account
                </button>
              </form>
            )}
            <div className="space-y-1">
              {accounts.map((a) => (
                <div
                  key={a.id}
                  className={`flex items-center justify-between rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised ${
                    !a.is_active ? "opacity-40" : ""
                  }`}
                >
                  <div>
                    <span className="text-sm font-medium text-text-primary">{a.name}</span>
                    <span className="ml-2 text-xs text-text-muted">
                      {a.account_type_name}
                    </span>
                    {!a.is_active && (
                      <span className="ml-2 text-xs text-danger">inactive</span>
                    )}
                  </div>
                  <div className="flex items-center gap-4">
                    <span className="text-sm tabular-nums text-text-primary">
                      {Number(a.balance).toLocaleString("en", {
                        minimumFractionDigits: 2,
                      })}{" "}
                      <span className="text-text-muted">{a.currency}</span>
                    </span>
                    <div className="flex gap-3">
                      <button
                        onClick={() => handleToggleActive(a)}
                        className="text-xs text-text-muted hover:text-text-secondary"
                      >
                        {a.is_active ? "Deactivate" : "Activate"}
                      </button>
                      <button
                        onClick={() => handleDeleteAccount(a.id)}
                        className="text-xs text-text-muted hover:text-danger"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                </div>
              ))}
              {accounts.length === 0 && (
                <p className="py-4 text-center text-sm text-text-muted">
                  {accountTypes.length === 0
                    ? "Create an account type first."
                    : "No accounts yet. Click '+ Add Account' above."}
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
