"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, formatLocalDate, todayISO } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, pageTitle, error as errorCls } from "@/lib/styles";
import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, Cell, Tooltip, ResponsiveContainer } from "recharts";
import CategorySelect from "@/components/ui/CategorySelect";
import type { Account, Budget, Category, Transaction } from "@/lib/types";

interface Forecast {
  period_start: string;
  period_end: string;
  executed_income: string;
  executed_expense: string;
  executed_net: string;
  pending_income: string;
  pending_expense: string;
  recurring_income: string;
  recurring_expense: string;
  forecast_income: string;
  forecast_expense: string;
  forecast_net: string;
  categories: { category_id: number; category_name: string; executed: string; pending: string; recurring: string; forecast: string }[];
}

interface BillingPeriod {
  id: number;
  start_date: string;
  end_date: string | null;
}

const PAGE_SIZE = 10;

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [allTransactions, setAllTransactions] = useState<Transaction[]>([]);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [period, setPeriod] = useState<BillingPeriod | null>(null);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [billingCycleDay, setBillingCycleDay] = useState(user?.billing_cycle_day ?? 1);
  const [periodIdx, setPeriodIdx] = useState(0);
  const [forecast, setForecast] = useState<Forecast | null>(null);
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

  const [chartFilter, setChartFilter] = useState<string | null>(null);
  const [dashSortField, setDashSortField] = useState<"date" | "description" | "amount">("date");
  const [dashSortDir, setDashSortDir] = useState<"asc" | "desc">("desc");

  // Selected period (navigate with arrows)
  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : period;
  const monthFrom = selectedPeriod?.start_date ?? formatLocalDate(new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  // For open periods, compute expected end from billing cycle day
  const cycleDay = billingCycleDay;
  let monthTo = selectedPeriod?.end_date ?? "";
  if (!monthTo && monthFrom) {
    const start = new Date(monthFrom + "T00:00:00");
    const nextMonth = new Date(start.getFullYear(), start.getMonth() + 1, cycleDay);
    nextMonth.setDate(nextMonth.getDate() - 1);
    monthTo = formatLocalDate(nextMonth);
  }

  const loadRefs = useCallback(async () => {
    const [accts, cats, bds, per, plist, bc] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<Budget[]>("/api/v1/budgets"),
      apiFetch<BillingPeriod>("/api/v1/settings/billing-period"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
      apiFetch<{ billing_cycle_day: number }>("/api/v1/settings/billing-cycle"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
    setBudgets(bds ?? []);
    if (bc) setBillingCycleDay(bc.billing_cycle_day);
    if (per) setPeriod(per);
    const pl = plist ?? [];
    setPeriods(pl);
    // Default to current period (open = no end_date), not index 0
    const currentIdx = pl.findIndex((p) => p.end_date === null);
    if (currentIdx >= 0) setPeriodIdx(currentIdx);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    const budgetUrl = monthFrom ? `/api/v1/budgets?period_start=${monthFrom}` : "/api/v1/budgets";
    const forecastUrl = monthFrom ? `/api/v1/forecast?period_start=${monthFrom}` : "/api/v1/forecast";
    const dateFilter = `date_from=${monthFrom}${monthTo ? `&date_to=${monthTo}` : ""}`;
    const [pageData, allData, bds, fc] = await Promise.all([
      apiFetch<Transaction[]>(`/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}&${dateFilter}`),
      p === 0 ? apiFetch<Transaction[]>(`/api/v1/transactions?limit=200&${dateFilter}`) : null,
      p === 0 ? apiFetch<Budget[]>(budgetUrl) : null,
      p === 0 ? apiFetch<Forecast>(forecastUrl) : null,
    ]);
    const page_txs = pageData ?? [];
    setHasMore(page_txs.length > PAGE_SIZE);
    setTransactions(page_txs.slice(0, PAGE_SIZE));
    if (allData) setAllTransactions(allData);
    if (bds) setBudgets(bds);
    if (fc) setForecast(fc);
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
      await loadRefs();
      await loadTransactions(0);
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
  const txMap = new Map(allTransactions.map((tx) => [tx.id, tx]));

  // Totals from ALL period transactions (not just the paginated page)
  const totalIncome = allTransactions.filter((tx) => tx.type === "income" && tx.status === "settled").reduce((s, tx) => s + Number(tx.amount), 0);
  const totalExpense = allTransactions.filter((tx) => tx.type === "expense" && tx.status === "settled").reduce((s, tx) => s + Number(tx.amount), 0);

  // Pending totals per account from all period transactions
  const pendingByAccount = allTransactions
    .filter((tx) => tx.status === "pending")
    .reduce<Record<number, number>>((acc, tx) => {
      const sign = tx.type === "income" ? 1 : -1;
      acc[tx.account_id] = (acc[tx.account_id] || 0) + Number(tx.amount) * sign;
      return acc;
    }, {});

  // Spending by category from all period transactions
  const spendingByCategory = allTransactions
    .filter((tx) => tx.type === "expense" && tx.status === "settled")
    .reduce<Record<string, number>>((acc, tx) => {
      acc[tx.category_name] = (acc[tx.category_name] || 0) + Number(tx.amount);
      return acc;
    }, {});
  const donutData = Object.entries(spendingByCategory)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value);

  // When chart filter is active, show from allTransactions; otherwise paginated
  const txSource = chartFilter ? allTransactions : transactions;

  // Dedup transfers
  const hiddenIds = new Set<number>();
  for (const tx of txSource) {
    if (tx.linked_transaction_id && tx.id > tx.linked_transaction_id) hiddenIds.add(tx.id);
  }
  const visibleTxs = txSource.filter((tx) => !hiddenIds.has(tx.id));


  function toggleDashSort(field: typeof dashSortField) {
    if (dashSortField === field) setDashSortDir(dashSortDir === "asc" ? "desc" : "asc");
    else { setDashSortField(field); setDashSortDir(field === "date" ? "desc" : "asc"); }
  }

  // Sort + filter the visible transactions
  const sortedVisibleTxs = visibleTxs
    .filter((tx) => !chartFilter || tx.category_name === chartFilter)
    .sort((a, b) => {
      let cmp = 0;
      if (dashSortField === "date") cmp = a.date.localeCompare(b.date);
      else if (dashSortField === "description") cmp = a.description.localeCompare(b.description);
      else if (dashSortField === "amount") cmp = Number(a.amount) - Number(b.amount);
      return dashSortDir === "asc" ? cmp : -cmp;
    });

  const CHART_COLORS = ["#D4A64A", "#5FA8D3", "#4ade80", "#f87171", "#a78bfa", "#fb923c", "#38bdf8", "#e879f9", "#34d399", "#fbbf24"];

  return (
    <AppShell>
      <div className="mb-6 flex items-center justify-between">
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
        <div className="space-y-5">
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
              <form onSubmit={handleQuickAdd} className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <div>
                  <label htmlFor="da-account" className={label}>{formMode === "transfer" ? "From" : "Account"}</label>
                  <select id="da-account" required value={formAccountId} onChange={(e) => handleAccountChange(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                    <option value="">Select account</option>
                    {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </div>
                {formMode === "transfer" ? (
                  <div>
                    <label htmlFor="da-to-account" className={label}>To</label>
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
                {formMode === "transaction" && (
                  <div>
                    <label htmlFor="da-category" className={label}>Category</label>
                    <CategorySelect id="da-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formType} className={input} />
                  </div>
                )}
                <div>
                  <label htmlFor="da-desc" className={label}>Description</label>
                  <input id="da-desc" type="text" required={formMode === "transaction"} placeholder={formMode === "transfer" ? "Transfer (optional)" : "What was it for?"} value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
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
                {formMode === "transaction" ? (
                  <div className="flex items-end gap-3">
                    <label className="flex items-center gap-2 text-sm text-text-secondary">
                      <input type="checkbox" checked={formRecurring} onChange={(e) => setFormRecurring(e.target.checked)} className="rounded border-border" />
                      Repeats
                    </label>
                    {formRecurring && (
                      <select value={formFrequency} onChange={(e) => setFormFrequency(e.target.value)} aria-label="Frequency" className={`w-28 text-xs ${input}`}>
                        <option value="monthly">Monthly</option>
                        <option value="weekly">Weekly</option>
                        <option value="yearly">Yearly</option>
                      </select>
                    )}
                  </div>
                ) : (
                  <div className="flex items-end">
                    <button type="submit" className={btnPrimary}>Transfer</button>
                  </div>
                )}
                {formMode === "transaction" && (
                  <div className="flex items-end">
                    <button type="submit" className={btnPrimary}>Add</button>
                  </div>
                )}
              </form>
            </div>
          )}

          {/* ═══ BILLING PERIOD — standalone nav bar ═══ */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <button onClick={() => { setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1)); setChartFilter(null); }} disabled={periodIdx >= periods.length - 1} className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Previous period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
              </button>
              <span className="text-sm font-medium text-text-primary">
                {monthFrom}{monthTo ? ` — ${monthTo}` : ""}
              </span>
              <button onClick={() => { setPeriodIdx(Math.max(periodIdx - 1, 0)); setChartFilter(null); }} disabled={periodIdx <= 0} className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Next period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
              </button>
              {selectedPeriod?.end_date === null && <span className="ml-1 rounded bg-success-dim px-2 py-0.5 text-[10px] font-semibold text-success">CURRENT</span>}
              {selectedPeriod?.end_date !== null && (
                <button onClick={() => { const idx = periods.findIndex((p) => p.end_date === null); if (idx >= 0) { setPeriodIdx(idx); setChartFilter(null); } }} className="ml-1 rounded-md px-2 py-1 text-[11px] font-medium text-text-muted hover:bg-surface-raised">Today</button>
              )}
            </div>
            <Link href="/transactions" className="text-xs text-accent hover:text-accent-hover">View All Transactions</Link>
          </div>

          {/* ═══ ROW 1: Executed | Forecast — two symmetric columns ═══ */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Executed */}
            <div className={`${card} p-5`}>
              <h2 className={`mb-4 ${cardTitle}`}>Executed</h2>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">Income</p>
                  <p className="mt-1 text-xl font-semibold tabular-nums text-success">+{formatAmount(totalIncome)}</p>
                </div>
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">Expenses</p>
                  <p className="mt-1 text-xl font-semibold tabular-nums text-danger">-{formatAmount(totalExpense)}</p>
                </div>
              </div>
              <div className="mt-4 pt-3 border-t border-border-subtle">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">Net</p>
                <p className={`mt-1 font-display text-2xl tabular-nums ${totalIncome - totalExpense >= 0 ? "text-accent" : "text-danger"}`}>
                  {totalIncome - totalExpense >= 0 ? "+" : ""}{formatAmount(totalIncome - totalExpense)}
                </p>
              </div>
            </div>

            {/* Forecast */}
            <div className={`${card} p-5`}>
              <h2 className={`mb-4 ${cardTitle}`}>Forecast</h2>
              {forecast ? (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">Income</p>
                      <p className="mt-1 text-xl font-semibold tabular-nums text-text-primary">{formatAmount(forecast.forecast_income)}</p>
                      {Number(forecast.pending_income) > 0 && <p className="text-[10px] text-text-muted">+{formatAmount(forecast.pending_income)} pending</p>}
                      {Number(forecast.recurring_income) > 0 && <p className="text-[10px] text-text-muted">+{formatAmount(forecast.recurring_income)} recurring</p>}
                    </div>
                    <div>
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">Expenses</p>
                      <p className="mt-1 text-xl font-semibold tabular-nums text-text-primary">{formatAmount(forecast.forecast_expense)}</p>
                      {Number(forecast.pending_expense) > 0 && <p className="text-[10px] text-text-muted">+{formatAmount(forecast.pending_expense)} pending</p>}
                      {Number(forecast.recurring_expense) > 0 && <p className="text-[10px] text-text-muted">+{formatAmount(forecast.recurring_expense)} recurring</p>}
                    </div>
                  </div>
                  <div className="mt-4 pt-3 border-t border-border-subtle">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">Projected Net</p>
                    <p className={`mt-1 font-display text-2xl tabular-nums ${Number(forecast.forecast_net) >= 0 ? "text-accent" : "text-danger"}`}>
                      {Number(forecast.forecast_net) >= 0 ? "+" : ""}{formatAmount(forecast.forecast_net)}
                    </p>
                  </div>
                </>
              ) : (
                <p className="text-sm text-text-muted py-4">No forecast data</p>
              )}
            </div>
          </div>

          {/* ═══ ROW 2: Accounts — single row, primary slightly bigger ═══ */}
          {accountsWithBalance.length > 0 && (() => {
            const defaultAcct = accountsWithBalance.find((a) => a.is_default);
            const others = accountsWithBalance.filter((a) => !a.is_default);
            return (
              <div className="flex gap-3 overflow-x-auto pb-1">
                {/* Primary account — wider */}
                {defaultAcct && (
                  <div className={`${card} px-5 py-3 shrink-0`} style={{ minWidth: "220px" }}>
                    <div className="flex items-center gap-2">
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-accent">{defaultAcct.name}</p>
                      <span className="rounded bg-accent-dim px-1.5 py-0.5 text-[9px] font-semibold text-accent">PRIMARY</span>
                    </div>
                    <p className="mt-1 text-xl font-semibold tabular-nums text-text-primary">{formatAmount(defaultAcct.balance)} <span className="text-xs text-text-muted">{defaultAcct.currency}</span></p>
                    {pendingByAccount[defaultAcct.id] !== undefined && pendingByAccount[defaultAcct.id] !== 0 && (
                      <p className="text-[10px] tabular-nums text-text-muted">Pending: {formatAmount(Math.abs(pendingByAccount[defaultAcct.id]))}</p>
                    )}
                  </div>
                )}
                {/* Other accounts */}
                {others.map((acct) => {
                  const pending = pendingByAccount[acct.id] || 0;
                  const isCreditCard = acct.account_type_slug === "credit_card";
                  return (
                    <div key={acct.id} className={`${card} px-4 py-3 shrink-0`} style={{ minWidth: "150px" }}>
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted truncate">{acct.name}</p>
                      <p className="mt-1 text-base font-semibold tabular-nums text-text-primary">{formatAmount(acct.balance)}</p>
                      {isCreditCard && pending !== 0 && (
                        <p className="text-[10px] tabular-nums text-danger">Pending: {formatAmount(Math.abs(pending))}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })()}

          {/* ═══ ROW 3: Three equal charts ═══ */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {/* Spending by category (donut) */}
            <div className={`${card} p-5`}>
              <h2 className={`mb-3 ${cardTitle}`}>Spending by Category</h2>
              {chartFilter && (
                <button onClick={() => setChartFilter(null)} className="mb-2 rounded-md bg-accent-dim px-2.5 py-1 text-xs text-accent hover:bg-accent/20">
                  Filtering: {chartFilter} &times;
                </button>
              )}
              {donutData.length > 0 ? (
                <div className="flex items-center gap-4">
                  <div className="w-40 h-40">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie
                          data={donutData} cx="50%" cy="50%" innerRadius={35} outerRadius={65}
                          paddingAngle={2} dataKey="value" stroke="none" cursor="pointer"
                          onClick={(_, idx) => {
                            const name = donutData[idx]?.name;
                            setChartFilter(chartFilter === name ? null : name);
                          }}
                        >
                          {donutData.map((d, i) => (
                            <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]}
                              opacity={chartFilter && chartFilter !== d.name ? 0.3 : 1} />
                          ))}
                        </Pie>
                        <Tooltip formatter={(v) => formatAmount(Number(v))} contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "6px", fontSize: "12px" }} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="flex-1 space-y-1.5">
                    {donutData.map((d, i) => (
                      <button key={d.name} onClick={() => setChartFilter(chartFilter === d.name ? null : d.name)}
                        className={`flex w-full items-center justify-between rounded px-1.5 py-0.5 transition-colors hover:bg-surface-raised ${chartFilter === d.name ? "bg-accent-dim" : ""}`}>
                        <div className="flex items-center gap-2">
                          <div className="h-2.5 w-2.5 rounded-full" style={{ background: CHART_COLORS[i % CHART_COLORS.length] }} />
                          <span className="text-xs text-text-secondary">{d.name}</span>
                        </div>
                        <span className="text-xs tabular-nums text-text-muted">{formatAmount(d.value)}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <p className="text-sm text-text-muted py-6 text-center">No expense data yet</p>
              )}
            </div>

            {/* Budget progress */}
            <div className={card}>
              <div className={`flex items-center justify-between ${cardHeader}`}>
                <h2 className={cardTitle}>Budget Progress</h2>
                <Link href="/budgets" className="text-xs text-accent hover:text-accent-hover">Manage</Link>
              </div>
              {budgets.length > 0 ? (
                <div className="p-4" style={{ height: Math.max(budgets.slice(0, 6).length * 40, 100) }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={budgets.slice(0, 6).map((b) => ({
                      name: b.category_name,
                      spent: Number(b.spent),
                      remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
                      pct: b.percent_used,
                    }))} layout="vertical" margin={{ left: 0, right: 0, top: 0, bottom: 0 }}>
                      <XAxis type="number" hide />
                      <YAxis type="category" dataKey="name" width={100} tick={{ fill: "var(--color-text-secondary)", fontSize: 11 }} />
                      <Tooltip
                        formatter={(v, name) => [formatAmount(Number(v)), name === "spent" ? "Spent" : "Remaining"]}
                        contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "6px", fontSize: "11px" }}
                      />
                      <Bar dataKey="spent" stackId="a" radius={[4, 0, 0, 4]} animationDuration={600}>
                        {budgets.slice(0, 6).map((b, i) => (
                          <Cell key={i} fill={b.percent_used > 100 ? "#f87171" : b.percent_used > 80 ? "#f59e0b" : "#4ade80"} />
                        ))}
                      </Bar>
                      <Bar dataKey="remaining" stackId="a" fill="var(--color-surface-overlay)" radius={[0, 4, 4, 0]} animationDuration={600} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="px-5 py-6 text-center text-sm text-text-muted">
                  No budgets set. <Link href="/budgets" className="text-accent">Add one</Link>
                </div>
              )}
            </div>

            {/* Forecast comparison — executed vs forecast per category */}
            <div className={`${card} p-5`}>
              <h2 className={`mb-3 ${cardTitle}`}>Forecast by Category</h2>
              {forecast && forecast.categories.length > 0 ? (
                <div style={{ height: Math.max(Math.min(forecast.categories.length, 8) * 32, 100) }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={forecast.categories.slice(0, 8).map((c) => ({
                        name: c.category_name.length > 12 ? c.category_name.slice(0, 12) + "…" : c.category_name,
                        executed: Number(c.executed),
                        pending: Number(c.pending),
                        recurring: Number(c.recurring),
                      }))}
                      layout="vertical"
                      margin={{ left: 0, right: 0, top: 0, bottom: 0 }}
                    >
                      <XAxis type="number" hide />
                      <YAxis type="category" dataKey="name" width={90} tick={{ fill: "var(--color-text-secondary)", fontSize: 10 }} />
                      <Tooltip
                        formatter={(v, name) => [formatAmount(Number(v)), name === "executed" ? "Executed" : name === "pending" ? "Pending" : "Recurring"]}
                        contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "6px", fontSize: "11px" }}
                      />
                      <Bar dataKey="executed" stackId="a" fill="#4ade80" radius={[3, 0, 0, 3]} animationDuration={600} />
                      <Bar dataKey="pending" stackId="a" fill="#D4A64A" animationDuration={600} />
                      <Bar dataKey="recurring" stackId="a" fill="#5FA8D3" radius={[0, 3, 3, 0]} animationDuration={600} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <p className="text-sm text-text-muted py-6 text-center">No forecast data</p>
              )}
              <div className="mt-2 flex gap-3 text-[10px] text-text-muted">
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-success" /> Executed</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "#D4A64A" }} /> Pending</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "#5FA8D3" }} /> Recurring</span>
              </div>
            </div>
          </div>

          {/* Recent transactions */}
          <div className={card}>
            <div className={`flex items-center justify-between ${cardHeader}`}>
              <h2 className={cardTitle}>Recent Transactions</h2>
            </div>
            {/* Sortable mini-header */}
            <div className="flex items-center justify-between px-5 py-1.5 border-b border-border-subtle text-[10px] font-semibold uppercase tracking-wider text-text-muted">
              <div className="flex items-center gap-3">
                <button onClick={() => toggleDashSort("date")} className="w-16 text-left hover:text-text-primary">Date{dashSortField === "date" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
                <button onClick={() => toggleDashSort("description")} className="text-left hover:text-text-primary">Description{dashSortField === "description" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
              </div>
              <button onClick={() => toggleDashSort("amount")} className="hover:text-text-primary">Amount{dashSortField === "amount" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
            </div>
            <div className="divide-y divide-border-subtle">
              {sortedVisibleTxs.map((tx) => {
                const isTransfer = tx.linked_transaction_id !== null;
                const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;
                return (
                  <div key={tx.id} className="flex items-center justify-between px-5 py-2.5">
                    <div className="flex items-center gap-3 min-w-0">
                      <span className="text-xs tabular-nums text-text-muted w-16 shrink-0">{tx.date.slice(5)}</span>
                      <div className="min-w-0">
                        <p className="text-sm text-text-primary truncate">{tx.description}</p>
                        <p className="text-[11px] text-text-muted truncate">
                          {isTransfer && linkedTx ? <>{tx.account_name} &rarr; {linkedTx.account_name}</> : <>{tx.account_name} · {tx.category_name}</>}
                          {tx.status === "pending" && <span className="ml-1 rounded bg-surface-overlay px-1 py-0.5 text-[9px] font-medium text-text-muted">pending</span>}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className={`text-sm font-medium tabular-nums ${isTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                        {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                      </span>
                      {!isTransfer && (
                        <button onClick={async () => { try { await apiFetch(`/api/v1/transactions/${tx.id}`, { method: "PUT", body: JSON.stringify({ status: tx.status === "settled" ? "pending" : "settled" }) }); await loadTransactions(page); } catch (err) { setError(extractErrorMessage(err)); } }} aria-label={`Toggle status`} className={`rounded px-1 py-0.5 text-[9px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-surface-overlay text-text-muted"}`}>
                          {tx.status}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
              {transactions.length === 0 && (
                <div className="px-5 py-6 text-center text-sm text-text-muted">
                  {!canAdd ? "Create accounts and categories first." : "No transactions this period."}
                </div>
              )}
            </div>
            {!chartFilter && (page > 0 || hasMore) && (
              <div className="flex items-center justify-between border-t border-border px-5 py-2.5">
                <button onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised disabled:opacity-40">Prev</button>
                <span className="text-[11px] text-text-muted">Page {page + 1}</span>
                <button onClick={() => setPage(page + 1)} disabled={!hasMore} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised disabled:opacity-40">Next</button>
              </div>
            )}
          </div>

          {activeAccounts.length === 0 && (
            <div className={`${card} p-10 text-center`}>
              <p className="text-text-secondary">No accounts yet.</p>
              <p className="mt-2 text-sm text-text-muted">
                Go to <Link href="/accounts" className="text-accent hover:text-accent-hover">Accounts</Link> to get started.
              </p>
            </div>
          )}
        </div>
      )}
    </AppShell>
  );
}
