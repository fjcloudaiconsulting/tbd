"use client";

import { FormEvent, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { apiFetch } from "@/lib/api";
import type { Account, AccountType } from "@/lib/types";

export default function AccountsPage() {
  const [accountTypes, setAccountTypes] = useState<AccountType[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);

  // Account type form
  const [typeName, setTypeName] = useState("");
  const [editingTypeId, setEditingTypeId] = useState<number | null>(null);
  const [editingTypeName, setEditingTypeName] = useState("");

  // Account form
  const [showAccountForm, setShowAccountForm] = useState(false);
  const [acctName, setAcctName] = useState("");
  const [acctTypeId, setAcctTypeId] = useState<number | "">("");
  const [acctBalance, setAcctBalance] = useState("0.00");
  const [acctCurrency, setAcctCurrency] = useState("EUR");

  const [error, setError] = useState("");

  const reload = async () => {
    const [types, accts] = await Promise.all([
      apiFetch<AccountType[]>("/api/v1/account-types"),
      apiFetch<Account[]>("/api/v1/accounts"),
    ]);
    setAccountTypes(types);
    setAccounts(accts);
  };

  useEffect(() => {
    reload().catch(() => {});
  }, []);

  // Account type handlers
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
    setError("");
    try {
      await apiFetch(`/api/v1/account-types/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  // Account handlers
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

  return (
    <AppShell>
      <h1 className="mb-6 text-xl font-semibold">Accounts</h1>

      {error && (
        <div className="mb-4 rounded bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Account Types */}
        <div className="rounded-lg border border-gray-200 bg-white">
          <div className="border-b border-gray-100 px-5 py-3">
            <h2 className="text-sm font-medium text-gray-700">Account Types</h2>
          </div>
          <div className="p-5">
            <form onSubmit={handleAddType} className="mb-4 flex gap-2">
              <input
                type="text"
                required
                placeholder="New type name"
                value={typeName}
                onChange={(e) => setTypeName(e.target.value)}
                className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
              />
              <button
                type="submit"
                className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
              >
                Add
              </button>
            </form>
            <div className="space-y-2">
              {accountTypes.map((at) => (
                <div
                  key={at.id}
                  className="flex items-center justify-between rounded border border-gray-100 px-3 py-2"
                >
                  {editingTypeId === at.id ? (
                    <div className="flex flex-1 gap-2">
                      <input
                        type="text"
                        value={editingTypeName}
                        onChange={(e) => setEditingTypeName(e.target.value)}
                        className="flex-1 rounded border border-gray-300 px-2 py-1 text-sm focus:border-blue-500 focus:outline-none"
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleUpdateType(at.id);
                          if (e.key === "Escape") setEditingTypeId(null);
                        }}
                      />
                      <button
                        onClick={() => handleUpdateType(at.id)}
                        className="text-sm text-blue-600 hover:underline"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingTypeId(null)}
                        className="text-sm text-gray-400 hover:underline"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <div>
                        <span className="text-sm">{at.name}</span>
                        <span className="ml-2 text-xs text-gray-400">
                          ({at.account_count})
                        </span>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => {
                            setEditingTypeId(at.id);
                            setEditingTypeName(at.name);
                          }}
                          className="text-xs text-blue-600 hover:underline"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDeleteType(at.id)}
                          className="text-xs text-red-500 hover:underline"
                        >
                          Delete
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
              {accountTypes.length === 0 && (
                <p className="text-sm text-gray-400">
                  No account types yet. Add one above.
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Accounts */}
        <div className="rounded-lg border border-gray-200 bg-white">
          <div className="flex items-center justify-between border-b border-gray-100 px-5 py-3">
            <h2 className="text-sm font-medium text-gray-700">Accounts</h2>
            {accountTypes.length > 0 && (
              <button
                onClick={() => setShowAccountForm(!showAccountForm)}
                className="text-xs text-blue-600 hover:underline"
              >
                {showAccountForm ? "Cancel" : "+ Add Account"}
              </button>
            )}
          </div>
          <div className="p-5">
            {showAccountForm && (
              <form onSubmit={handleAddAccount} className="mb-4 space-y-3">
                <input
                  type="text"
                  required
                  placeholder="Account name"
                  value={acctName}
                  onChange={(e) => setAcctName(e.target.value)}
                  className="w-full rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 focus:outline-none"
                />
                <select
                  required
                  value={acctTypeId}
                  onChange={(e) => setAcctTypeId(Number(e.target.value))}
                  className="w-full rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
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
                    className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
                  />
                  <input
                    type="text"
                    maxLength={3}
                    value={acctCurrency}
                    onChange={(e) =>
                      setAcctCurrency(e.target.value.toUpperCase())
                    }
                    className="w-16 rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
                  />
                </div>
                <button
                  type="submit"
                  className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
                >
                  Create Account
                </button>
              </form>
            )}
            <div className="space-y-2">
              {accounts.map((a) => (
                <div
                  key={a.id}
                  className={`flex items-center justify-between rounded border px-3 py-2 ${
                    a.is_active
                      ? "border-gray-100"
                      : "border-gray-100 bg-gray-50 opacity-60"
                  }`}
                >
                  <div>
                    <span className="text-sm font-medium">{a.name}</span>
                    <span className="ml-2 text-xs text-gray-400">
                      {a.account_type_name}
                    </span>
                    {!a.is_active && (
                      <span className="ml-2 text-xs text-orange-500">
                        inactive
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-medium">
                      {Number(a.balance).toLocaleString("en", {
                        minimumFractionDigits: 2,
                      })}{" "}
                      <span className="text-xs text-gray-400">{a.currency}</span>
                    </span>
                    <button
                      onClick={() => handleToggleActive(a)}
                      className="text-xs text-gray-500 hover:underline"
                    >
                      {a.is_active ? "Deactivate" : "Activate"}
                    </button>
                    <button
                      onClick={() => handleDeleteAccount(a.id)}
                      className="text-xs text-red-500 hover:underline"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              ))}
              {accounts.length === 0 && (
                <p className="text-sm text-gray-400">
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
