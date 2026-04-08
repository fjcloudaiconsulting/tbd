"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import {
  input,
  label,
  btnPrimary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
  btnLink,
  btnDanger,
} from "@/lib/styles";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
  Legend,
} from "recharts";
import type { Category, ForecastPlan, ForecastPlanItem } from "@/lib/types";

interface BillingPeriod {
  id: number;
  start_date: string;
  end_date: string | null;
}

const SOURCE_LABELS: Record<string, string> = {
  manual: "Manual",
  recurring: "Recurring",
  history: "Avg (3mo)",
};

export default function ForecastPlansPage() {
  const { user, loading } = useAuth();
  const [plan, setPlan] = useState<ForecastPlan | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [periodIdx, setPeriodIdx] = useState(0);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);

  // Add form
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formAmount, setFormAmount] = useState("");

  // Edit
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editAmount, setEditAmount] = useState("");

  // View filter
  const [viewFilter, setViewFilter] = useState<"all" | "income" | "expense">("all");

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : null;
  const periodStart = selectedPeriod?.start_date ?? "";
  const isCurrentPeriod = periodIdx === 0;

  const loadRefs = useCallback(async () => {
    const [c, p] = await Promise.all([
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
    ]);
    setCategories(c ?? []);
    setPeriods(p ?? []);
  }, []);

  const loadPlan = useCallback(async () => {
    const url = periodStart
      ? `/api/v1/forecast-plans?period_start=${periodStart}`
      : "/api/v1/forecast-plans";
    const p = await apiFetch<ForecastPlan>(url);
    setPlan(p);
    setFetching(false);
  }, [periodStart]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadPlan().catch(() => setFetching(false));
    }
  }, [loading, user, loadPlan]);

  // Available categories for add form
  const masterCategories = categories.filter((c) => c.parent_id === null);
  const existingKeys = new Set(
    (plan?.items ?? []).map((i) => `${i.category_id}-${i.type}`)
  );
  const availableForType = masterCategories.filter(
    (c) =>
      !existingKeys.has(`${c.id}-${formType}`) &&
      (formType === "expense"
        ? c.type === "expense" || c.type === "both"
        : c.type === "income" || c.type === "both")
  );

  // Filtered items
  const items = (plan?.items ?? []).filter(
    (i) => viewFilter === "all" || i.type === viewFilter
  );

  // Grouped by type for display
  const incomeItems = items.filter((i) => i.type === "income");
  const expenseItems = items.filter((i) => i.type === "expense");

  async function handlePopulate() {
    setError("");
    try {
      const url = periodStart
        ? `/api/v1/forecast-plans/populate?period_start=${periodStart}`
        : "/api/v1/forecast-plans/populate";
      const p = await apiFetch<ForecastPlan>(url, { method: "POST" });
      setPlan(p);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleAddItem(e: FormEvent) {
    e.preventDefault();
    if (!plan) return;
    setError("");
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/${plan.id}/items`,
        {
          method: "POST",
          body: JSON.stringify({
            category_id: formCategoryId,
            type: formType,
            planned_amount: parseFloat(formAmount),
          }),
        }
      );
      setPlan(p);
      setFormCategoryId("");
      setFormAmount("");
      setShowForm(false);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleUpdateItem(itemId: number) {
    if (!plan) return;
    setError("");
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/${plan.id}/items/${itemId}`,
        {
          method: "PUT",
          body: JSON.stringify({ planned_amount: parseFloat(editAmount) }),
        }
      );
      setPlan(p);
      setEditingId(null);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleDeleteItem(itemId: number) {
    if (!plan || !confirm("Remove this plan item?")) return;
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/${plan.id}/items/${itemId}`,
        { method: "DELETE" }
      );
      setPlan(p);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleActivate() {
    if (!plan || !confirm("Mark this plan as active? You can still edit items."))
      return;
    setError("");
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/${plan.id}/activate`,
        { method: "POST" }
      );
      setPlan(p);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  // Chart data — expense items only
  const chartData = expenseItems.map((i) => ({
    name: i.category_name,
    planned: Number(i.planned_amount),
    actual: Number(i.actual_amount),
  }));

  const plannedNet =
    Number(plan?.total_planned_income ?? 0) -
    Number(plan?.total_planned_expense ?? 0);
  const actualNet =
    Number(plan?.total_actual_income ?? 0) -
    Number(plan?.total_actual_expense ?? 0);

  return (
    <AppShell>
      <div className="mb-6 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Forecast Plans</h1>
        <div className="flex gap-2">
          <button onClick={handlePopulate} className={btnPrimary}>
            Auto-populate
          </button>
          <button
            onClick={() => setShowForm(!showForm)}
            className={btnPrimary}
          >
            {showForm ? "Cancel" : "+ Add Item"}
          </button>
        </div>
      </div>

      {/* Period navigation */}
      {periods.length > 0 && (
        <div className="mb-5 flex items-center gap-3">
          <button
            onClick={() =>
              setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1))
            }
            disabled={periodIdx >= periods.length - 1}
            className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30"
            aria-label="Older period"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M15.75 19.5 8.25 12l7.5-7.5"
              />
            </svg>
          </button>
          <span className="text-sm text-text-secondary">
            {selectedPeriod?.start_date}
            {selectedPeriod?.end_date
              ? ` — ${selectedPeriod.end_date}`
              : ""}
            {isCurrentPeriod && (
              <span className="ml-2 text-xs font-medium text-success">
                current
              </span>
            )}
          </span>
          <button
            onClick={() => setPeriodIdx(Math.max(periodIdx - 1, 0))}
            disabled={periodIdx <= 0}
            className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30"
            aria-label="Newer period"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="m8.25 4.5 7.5 7.5-7.5 7.5"
              />
            </svg>
          </button>
          {plan && (
            <span
              className={`ml-3 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                plan.status === "active"
                  ? "bg-success/15 text-success"
                  : "bg-accent/15 text-accent"
              }`}
            >
              {plan.status}
            </span>
          )}
        </div>
      )}

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {/* Add item form */}
      {showForm && (
        <div className={`mb-6 ${card} p-6`}>
          <form
            onSubmit={handleAddItem}
            className="flex flex-wrap items-end gap-4"
          >
            <div className="w-32">
              <label htmlFor="fp-type" className={label}>
                Type
              </label>
              <select
                id="fp-type"
                value={formType}
                onChange={(e) =>
                  setFormType(e.target.value as "income" | "expense")
                }
                className={input}
              >
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </div>
            <div className="min-w-[200px] flex-1">
              <label htmlFor="fp-cat" className={label}>
                Category
              </label>
              <select
                id="fp-cat"
                required
                value={formCategoryId}
                onChange={(e) =>
                  setFormCategoryId(
                    e.target.value === "" ? "" : Number(e.target.value)
                  )
                }
                className={input}
              >
                <option value="">Select category</option>
                {availableForType.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="w-40">
              <label htmlFor="fp-amount" className={label}>
                Planned Amount
              </label>
              <input
                id="fp-amount"
                type="number"
                step="0.01"
                min="0.01"
                required
                placeholder="0.00"
                value={formAmount}
                onChange={(e) => setFormAmount(e.target.value)}
                className={input}
              />
            </div>
            <button type="submit" className={btnPrimary}>
              Add
            </button>
          </form>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Summary cards */}
          {plan && plan.items.length > 0 && (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Planned Income</p>
                <p className="mt-1 text-xl font-semibold tabular-nums text-success">
                  {formatAmount(plan.total_planned_income)}
                </p>
                <p className="mt-0.5 text-xs text-text-muted">
                  Actual: {formatAmount(plan.total_actual_income)}
                </p>
              </div>
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Planned Expenses</p>
                <p className="mt-1 text-xl font-semibold tabular-nums text-danger">
                  {formatAmount(plan.total_planned_expense)}
                </p>
                <p className="mt-0.5 text-xs text-text-muted">
                  Actual: {formatAmount(plan.total_actual_expense)}
                </p>
              </div>
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Planned Net</p>
                <p
                  className={`mt-1 text-xl font-semibold tabular-nums ${plannedNet >= 0 ? "text-success" : "text-danger"}`}
                >
                  {formatAmount(plannedNet)}
                </p>
              </div>
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Actual Net</p>
                <p
                  className={`mt-1 text-xl font-semibold tabular-nums ${actualNet >= 0 ? "text-success" : "text-danger"}`}
                >
                  {formatAmount(actualNet)}
                </p>
              </div>
            </div>
          )}

          {/* Planned vs Actual chart */}
          {chartData.length > 0 && (
            <div className={`${card} p-5`}>
              <h2 className={`${cardTitle} mb-4`}>
                Planned vs Actual (Expenses)
              </h2>
              <div style={{ height: Math.max(chartData.length * 52, 120) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={chartData}
                    layout="vertical"
                    margin={{ left: 10, right: 10, top: 0, bottom: 0 }}
                  >
                    <XAxis type="number" hide />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={130}
                      tick={{
                        fill: "var(--color-text-secondary)",
                        fontSize: 12,
                      }}
                    />
                    <Tooltip
                      formatter={(v: number, name: string) => [
                        formatAmount(v),
                        name === "planned" ? "Planned" : "Actual",
                      ]}
                      contentStyle={{
                        background: "var(--color-surface)",
                        border: "1px solid var(--color-border)",
                        borderRadius: "6px",
                        fontSize: "12px",
                      }}
                    />
                    <Legend
                      formatter={(v) =>
                        v === "planned" ? "Planned" : "Actual"
                      }
                      wrapperStyle={{ fontSize: "12px" }}
                    />
                    <Bar
                      dataKey="planned"
                      fill="var(--color-accent)"
                      radius={[4, 4, 4, 4]}
                      barSize={14}
                      animationDuration={800}
                    />
                    <Bar
                      dataKey="actual"
                      radius={[4, 4, 4, 4]}
                      barSize={14}
                      animationDuration={800}
                    >
                      {chartData.map((d, i) => (
                        <Cell
                          key={i}
                          fill={
                            d.actual > d.planned ? "#f87171" : "#4ade80"
                          }
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* Filter tabs */}
          {plan && plan.items.length > 0 && (
            <div className="flex gap-1">
              {(["all", "expense", "income"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setViewFilter(f)}
                  className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                    viewFilter === f
                      ? "bg-accent text-accent-text"
                      : "text-text-muted hover:bg-surface-raised"
                  }`}
                >
                  {f === "all"
                    ? "All"
                    : f === "income"
                      ? "Income"
                      : "Expenses"}
                </button>
              ))}
            </div>
          )}

          {/* Item list */}
          {(viewFilter === "all" || viewFilter === "income") &&
            incomeItems.length > 0 && (
              <ItemSection
                title="Income"
                items={incomeItems}
                editingId={editingId}
                editAmount={editAmount}
                onStartEdit={(item) => {
                  setEditingId(item.id);
                  setEditAmount(String(item.planned_amount));
                }}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={handleUpdateItem}
                onDelete={handleDeleteItem}
                setEditAmount={setEditAmount}
              />
            )}

          {(viewFilter === "all" || viewFilter === "expense") &&
            expenseItems.length > 0 && (
              <ItemSection
                title="Expenses"
                items={expenseItems}
                editingId={editingId}
                editAmount={editAmount}
                onStartEdit={(item) => {
                  setEditingId(item.id);
                  setEditAmount(String(item.planned_amount));
                }}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={handleUpdateItem}
                onDelete={handleDeleteItem}
                setEditAmount={setEditAmount}
              />
            )}

          {plan && plan.items.length === 0 && (
            <div className={`${card} px-6 py-12 text-center`}>
              <p className="text-sm text-text-muted">
                No plan items yet. Click{" "}
                <strong>&quot;Auto-populate&quot;</strong> to import from
                recurring templates and history, or{" "}
                <strong>&quot;+ Add Item&quot;</strong> manually.
              </p>
            </div>
          )}

          {/* Actions */}
          {plan && plan.items.length > 0 && plan.status === "draft" && (
            <div className="flex justify-end">
              <button onClick={handleActivate} className={btnPrimary}>
                Activate Plan
              </button>
            </div>
          )}
        </div>
      )}
    </AppShell>
  );
}

/* ── Item section component ──────────────────────────────────────────────── */

function ItemSection({
  title,
  items,
  editingId,
  editAmount,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onDelete,
  setEditAmount,
}: {
  title: string;
  items: ForecastPlanItem[];
  editingId: number | null;
  editAmount: string;
  onStartEdit: (item: ForecastPlanItem) => void;
  onCancelEdit: () => void;
  onSaveEdit: (id: number) => void;
  onDelete: (id: number) => void;
  setEditAmount: (v: string) => void;
}) {
  return (
    <div className={card}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>{title}</h2>
      </div>
      {/* Header row */}
      <div className="grid grid-cols-[1fr_100px_100px_100px_80px_100px] gap-2 px-6 py-2 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
        <span>Category</span>
        <span className="text-right">Planned</span>
        <span className="text-right">Actual</span>
        <span className="text-right">Variance</span>
        <span className="text-center">Source</span>
        <span className="text-right">Actions</span>
      </div>
      <div className="divide-y divide-border-subtle">
        {items.map((item) => {
          const variance = Number(item.variance);
          const isOver =
            item.type === "expense" ? variance > 0 : variance < 0;
          return (
            <div
              key={item.id}
              className="grid grid-cols-[1fr_100px_100px_100px_80px_100px] items-center gap-2 px-6 py-2.5"
            >
              {editingId === item.id ? (
                <>
                  <span className="text-sm text-text-primary">
                    {item.category_name}
                  </span>
                  <input
                    type="number"
                    step="0.01"
                    min="0.01"
                    value={editAmount}
                    onChange={(e) => setEditAmount(e.target.value)}
                    className={`text-right ${input}`}
                    autoFocus
                    onKeyDown={(e) => {
                      if (e.key === "Enter") onSaveEdit(item.id);
                      if (e.key === "Escape") onCancelEdit();
                    }}
                  />
                  <span className="text-right text-sm tabular-nums text-text-secondary">
                    {formatAmount(item.actual_amount)}
                  </span>
                  <span />
                  <span />
                  <div className="flex justify-end gap-2">
                    <button
                      onClick={() => onSaveEdit(item.id)}
                      className="text-xs text-accent hover:text-accent-hover"
                    >
                      Save
                    </button>
                    <button
                      onClick={onCancelEdit}
                      className="text-xs text-text-muted hover:text-text-secondary"
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <span className="text-sm text-text-primary">
                    {item.category_name}
                  </span>
                  <span className="text-right text-sm tabular-nums text-text-primary">
                    {formatAmount(item.planned_amount)}
                  </span>
                  <span className="text-right text-sm tabular-nums text-text-secondary">
                    {formatAmount(item.actual_amount)}
                  </span>
                  <span
                    className={`text-right text-sm tabular-nums font-medium ${
                      isOver ? "text-danger" : "text-success"
                    }`}
                  >
                    {variance > 0 ? "+" : ""}
                    {formatAmount(variance)}
                  </span>
                  <span className="text-center text-[11px] text-text-muted">
                    {SOURCE_LABELS[item.source] ?? item.source}
                  </span>
                  <div className="flex justify-end gap-2">
                    <button
                      onClick={() => onStartEdit(item)}
                      className={btnLink}
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => onDelete(item.id)}
                      className={btnDanger}
                    >
                      Remove
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
