"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, todayISO } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, pageTitle, error as errorCls } from "@/lib/styles";
import CategorySelect from "@/components/ui/CategorySelect";
import type { Account, Category, Transaction } from "@/lib/types";

function formatLocalDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function billingCycleRange(cycleDay: number): { from: string; to: string } {
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth();
  const d = now.getDate();

  let fromDate: Date;
  let toDate: Date;

  if (d >= cycleDay) {
    // We're in the current cycle: cycleDay this month → cycleDay next month - 1
    fromDate = new Date(y, m, cycleDay);
    toDate = new Date(y, m + 1, cycleDay - 1);
  } else {
    // We're before the cycle day: cycleDay last month → cycleDay this month - 1
    fromDate = new Date(y, m - 1, cycleDay);
    toDate = new Date(y, m, cycleDay - 1);
  }

  return {
    from: formatLocalDate(fromDate),
    to: formatLocalDate(toDate),
  };
}

const PAGE_SIZE = 10;

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [fetching, setFetching] = useState(true);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");

  // Quick-add form
  const [showForm, setShowForm] = useState(false);
  const [formMode, setFormMode] = useState<"transaction" | "transfer">("transaction");
  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formToAccountId, setFormToAccountId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formStatus, setFormStatus] = useState<"settled" | "pending">("settled");
  const [formDate, setFormDate] = useState(todayISO());
  const [formRecurring, setFormRecurring] = useState(false);
  const [formFrequency, setFormFrequency] = useState("monthly");
  const [formAutoSettle, setFormAutoSettle] = useState(false);

  const cycleDay = user?.billing_cycle_day ?? 1;
  const { from: monthFrom, to: monthTo } = billingCycleRange(cycleDay);

  const loadRefs = useCallback(async () => {
    const [accts, cats] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    const url = `/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}&date_from=${monthFrom}&date_to=${monthTo}`;
    const data = (await apiFetch<Transaction[]>(url)) ?? [];
    setHasMore(data.length > PAGE_SIZE);
    setTransactions(data.slice(0, PAGE_SIZE));
    setFetching(false);
  }, [monthFrom, monthTo]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, loadTransactions, page]);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

  async function handleQuickAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      if (formMode === "transfer") {
        await apiFetch("/api/v1/transactions/transfer", {
          method: "POST",
          body: JSON.stringify({
            from_account_id: formAccountId,
            to_account_id: formToAccountId,
            category_id: formCategoryId,
            description: formDescription,
            amount: formAmount,
            status: formStatus,
            date: formDate,
          }),
        });
      } else {
        await apiFetch("/api/v1/transactions", {
          method: "POST",
          body: JSON.stringify({
            account_id: formAccountId,
            category_id: formCategoryId,
            description: formDescription,
            amount: formAmount,
            type: formType,
            status: formStatus,
            date: formDate,
          }),
        });
        if (formRecurring && formMode === "transaction") {
          await apiFetch("/api/v1/recurring", {
            method: "POST",
            body: JSON.stringify({
              account_id: formAccountId,
              category_id: formCategoryId,
              description: formDescription,
              amount: formAmount,
              type: formType,
              frequency: formFrequency,
              next_due_date: formDate,
              auto_settle: formAutoSettle,
            }),
          });
        }
      }
      setFormDescription("");
      setFormAmount("");
      setFormType("expense");
      setFormStatus("settled");
      setFormToAccountId("");
      setFormRecurring(false);
      setFormAutoSettle(false);
      setFormDate(todayISO());
      setShowForm(false);
      await Promise.all([loadRefs(), loadTransactions(page)]);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  const activeAccounts = accounts.filter((a) => a.is_active);
  const defaultAccount = activeAccounts.find((a) => a.is_default);
  const canAdd = activeAccounts.length > 0 && categories.length > 0;

  // Pre-select default account when opening form
  useEffect(() => {
    if (showForm && formAccountId === "" && defaultAccount) {
      setFormAccountId(defaultAccount.id);
      if (defaultAccount.account_type_slug === "credit_card") setFormStatus("pending");
    }
  }, [showForm, formAccountId, defaultAccount]);

  function handleAccountChange(id: number | "") {
    setFormAccountId(id);
    if (formToAccountId === id) setFormToAccountId("");
    const acct = accounts.find((a) => a.id === id);
    setFormStatus(acct?.account_type_slug === "credit_card" ? "pending" : "settled");
  }

  // Total balance by currency (settled only — what's in the accounts)
  const balanceByCurrency = activeAccounts.reduce<Record<string, number>>(
    (acc, a) => {
      const cur = a.currency || "EUR";
      acc[cur] = (acc[cur] || 0) + Number(a.balance);
      return acc;
    },
    {}
  );
  const currencies = Object.entries(balanceByCurrency);

  // Accounts with balance != 0 for individual tiles
  const accountsWithBalance = activeAccounts.filter((a) => Number(a.balance) !== 0);

  // Precompute tx map for O(1) linked lookups
  const txMap = new Map(transactions.map((tx) => [tx.id, tx]));

  // Pending totals per account from current-month transactions
  const pendingByAccount = transactions
    .filter((tx) => tx.status === "pending")
    .reduce<Record<number, number>>((acc, tx) => {
      const sign = tx.type === "income" ? 1 : -1;
      acc[tx.account_id] = (acc[tx.account_id] || 0) + Number(tx.amount) * sign;
      return acc;
    }, {});

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Dashboard</h1>
        {canAdd && (
          <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
            {showForm ? "Cancel" : "+ Quick Add"}
          </button>
        )}
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Quick-add form */}
          {showForm && (
            <div className={`${card} p-6`}>
              <div className="mb-4 flex items-center gap-4">
                <h2 className={cardTitle}>{formMode === "transfer" ? "Quick Transfer" : "Quick Add"}</h2>
                <div className="flex rounded-md border border-border text-xs">
                  <button type="button" onClick={() => setFormMode("transaction")} className={`px-3 py-1 rounded-l-md ${formMode === "transaction" ? "bg-accent text-accent-text" : "text-text-muted hover:bg-surface-raised"}`}>Transaction</button>
                  <button type="button" onClick={() => setFormMode("transfer")} className={`px-3 py-1 rounded-r-md ${formMode === "transfer" ? "bg-accent text-accent-text" : "text-text-muted hover:bg-surface-raised"}`}>Transfer</button>
                </div>
              </div>
              <form onSubmit={handleQuickAdd} className="grid grid-cols-2 gap-4 lg:grid-cols-4">
                <div>
                  <label htmlFor="da-account" className={label}>{formMode === "transfer" ? "From Account" : "Account"}</label>
                  <select id="da-account" required value={formAccountId} onChange={(e) => handleAccountChange(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                    <option value="">Select account</option>
                    {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </div>
                {formMode === "transfer" ? (
                  <div>
                    <label htmlFor="da-to-account" className={label}>To Account</label>
                    <select id="da-to-account" required value={formToAccountId} onChange={(e) => setFormToAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                      <option value="">Select account</option>
                      {activeAccounts.filter((a) => a.id !== formAccountId).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                  </div>
                ) : (
                  <div>
                    <label htmlFor="da-type" className={label}>Type</label>
                    <select id="da-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                      <option value="expense">Expense</option>
                      <option value="income">Income</option>
                    </select>
                  </div>
                )}
                <div>
                  <label htmlFor="da-category" className={label}>Category</label>
                  <CategorySelect id="da-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formMode === "transfer" ? "expense" : formType} className={input} />
                </div>
                <div>
                  <label htmlFor="da-desc" className={label}>Description</label>
                  <input id="da-desc" type="text" required placeholder="What was it for?" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
                </div>
                <div>
                  <label htmlFor="da-amount" className={label}>Amount</label>
                  <input id="da-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
                </div>
                <div>
                  <label htmlFor="da-status" className={label}>Status</label>
                  <select id="da-status" value={formStatus} onChange={(e) => setFormStatus(e.target.value as "settled" | "pending")} className={input}>
                    <option value="settled">Settled</option>
                    <option value="pending">Pending</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="da-date" className={label}>Date</label>
                  <input id="da-date" type="date" required value={formDate} onChange={(e) => setFormDate(e.target.value)} className={input} />
                </div>
                {formMode === "transaction" && (
                  <div className="flex items-end gap-3">
                    <label className="flex items-center gap-2 text-sm text-text-secondary">
                      <input type="checkbox" checked={formRecurring} onChange={(e) => setFormRecurring(e.target.checked)} className="rounded border-border" />
                      Repeats
                    </label>
                    {formRecurring && (
                      <>
                        <select value={formFrequency} onChange={(e) => setFormFrequency(e.target.value)} aria-label="Frequency" className={`w-32 text-sm ${input}`}>
                          <option value="weekly">Weekly</option>
                          <option value="biweekly">Biweekly</option>
                          <option value="monthly">Monthly</option>
                          <option value="quarterly">Quarterly</option>
                          <option value="yearly">Yearly</option>
                        </select>
                        <label className="flex items-center gap-1 text-xs text-text-muted">
                          <input type="checkbox" checked={formAutoSettle} onChange={(e) => setFormAutoSettle(e.target.checked)} className="rounded border-border" />
                          Auto
                        </label>
                      </>
                    )}
                  </div>
                )}
                <div className="flex items-end">
                  <button type="submit" className={btnPrimary}>Add</button>
                </div>
              </form>
            </div>
          )}

          {/* Total balance */}
          {currencies.length > 0 && (
            <div className="flex gap-4">
              {currencies.map(([currency, total]) => (
                <div key={currency} className={`flex-1 ${card} p-6`}>
                  <p className={cardTitle}>Total Balance</p>
                  <p className="mt-2 font-display text-3xl text-accent">
                    {formatAmount(total)}
                    <span className="ml-2 text-lg text-text-muted">{currency}</span>
                  </p>
                </div>
              ))}
            </div>
          )}

          {/* Per-account tiles */}
          {accountsWithBalance.length > 0 && (
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {accountsWithBalance.map((acct) => {
                const pending = pendingByAccount[acct.id] || 0;
                const isCreditCard = acct.account_type_slug === "credit_card";
                return (
                  <div key={acct.id} className={`${card} p-4`}>
                    <p className="text-xs font-medium text-text-muted truncate">{acct.name}</p>
                    <p className="text-[11px] text-text-muted">{acct.account_type_name}</p>
                    <p className="mt-1.5 text-lg font-semibold tabular-nums text-text-primary">
                      {formatAmount(acct.balance)} <span className="text-xs text-text-muted">{acct.currency}</span>
                    </p>
                    {pending !== 0 && (
                      <p className={`mt-0.5 text-xs tabular-nums ${isCreditCard ? "text-danger" : "text-text-muted"}`}>
                        {isCreditCard ? "Pending charges: " : "Pending: "}
                        {formatAmount(Math.abs(pending))}
                      </p>
                    )}
                    {isCreditCard && pending !== 0 && (
                      <p className="mt-0.5 text-xs tabular-nums text-text-secondary">
                        Net: {formatAmount(Number(acct.balance) + pending)}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Recent transactions (current month) */}
          <div className={card}>
            <div className={`flex items-center justify-between ${cardHeader}`}>
              <h2 className={cardTitle}>Transactions — This Month</h2>
              <Link href="/transactions" className="text-xs text-accent hover:text-accent-hover">
                View All
              </Link>
            </div>
            <div className="divide-y divide-border-subtle">
              {(() => {
                // Deduplicate transfers: keep the expense side (lower id for stability)
                const hiddenIds = new Set<number>();
                for (const tx of transactions) {
                  if (tx.linked_transaction_id && tx.id > tx.linked_transaction_id) {
                    hiddenIds.add(tx.id);
                  }
                }
                return transactions.filter((tx) => !hiddenIds.has(tx.id)).map((tx) => {
                  const isTransfer = tx.linked_transaction_id !== null;
                  const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;

                  return (
                    <div key={tx.id} className="flex items-center justify-between px-6 py-3">
                      <div className="flex items-center gap-4">
                        <span className="text-sm tabular-nums text-text-muted w-20">{tx.date}</span>
                        <div>
                          <p className="text-sm text-text-primary">{tx.description}</p>
                          <p className="text-xs text-text-muted">
                            {isTransfer && linkedTx
                              ? <>{tx.account_name} &rarr; {linkedTx.account_name}</>
                              : <>{tx.account_name} · {tx.category_name}</>
                            }
                            {tx.status === "pending" && (
                              <span className="ml-1.5 rounded bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-text-muted">
                                pending
                              </span>
                            )}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`text-sm font-medium tabular-nums ${isTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                          {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                          {isTransfer && <span className="ml-1 text-xs text-text-muted">transfer</span>}
                        </span>
                        {!isTransfer && (
                          <button
                            onClick={async () => { try { await apiFetch(`/api/v1/transactions/${tx.id}`, { method: "PUT", body: JSON.stringify({ status: tx.status === "settled" ? "pending" : "settled" }) }); await Promise.all([loadRefs(), loadTransactions(page)]); } catch (err) { setError(extractErrorMessage(err)); } }}
                            aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
                            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-surface-overlay text-text-muted"}`}
                          >
                            {tx.status}
                          </button>
                        )}
                        <button
                          onClick={async () => { if (!confirm("Delete this transaction?")) return; try { await apiFetch(`/api/v1/transactions/${tx.id}`, { method: "DELETE" }); await Promise.all([loadRefs(), loadTransactions(page)]); } catch (err) { setError(extractErrorMessage(err)); } }}
                          aria-label={`Delete: ${tx.description}`}
                          className="text-xs text-text-muted hover:text-danger"
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  );
                });
              })()}
              {transactions.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  {!canAdd
                    ? "Create accounts and categories first."
                    : "No transactions this month."}
                </div>
              )}
            </div>

            {/* Pagination */}
            {(page > 0 || hasMore) && (
              <div className="flex items-center justify-between border-t border-border px-6 py-3">
                <button
                  onClick={() => setPage(Math.max(0, page - 1))}
                  disabled={page === 0}
                  className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-surface-raised disabled:opacity-40"
                >
                  Previous
                </button>
                <span className="text-xs text-text-muted">Page {page + 1}</span>
                <button
                  onClick={() => setPage(page + 1)}
                  disabled={!hasMore}
                  className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-surface-raised disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            )}
          </div>

          {/* Empty state for no accounts */}
          {activeAccounts.length === 0 && (
            <div className={`${card} p-10 text-center`}>
              <p className="text-text-secondary">No accounts yet.</p>
              <p className="mt-2 text-sm text-text-muted">
                Go to{" "}
                <Link href="/accounts" className="text-accent hover:text-accent-hover">Accounts</Link>{" "}
                to create your first account.
              </p>
            </div>
          )}
        </div>
      )}
    </AppShell>
  );
}
