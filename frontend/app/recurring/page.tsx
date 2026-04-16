"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { btnSecondary, card, cardHeader, cardTitle, error as errorCls, success as successCls, pageTitle } from "@/lib/styles";
import type { RecurringTransaction } from "@/lib/types";

const FREQ_LABELS: Record<string, string> = {
  weekly: "Weekly",
  biweekly: "Every 2 weeks",
  monthly: "Monthly",
  quarterly: "Quarterly",
  yearly: "Yearly",
};

export default function RecurringPage() {
  const { user, loading } = useAuth();
  const [items, setItems] = useState<RecurringTransaction[]>([]);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [confirmStop, setConfirmStop] = useState<{ id: number; description: string } | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    const data = await apiFetch<RecurringTransaction[]>("/api/v1/recurring");
    setItems(data ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  async function handleStop(item: RecurringTransaction) {
    setConfirmStop({ id: item.id, description: item.description });
  }

  async function doStop(id: number, description: string) {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ pending_removed: number }>(`/api/v1/recurring/${id}/stop`, { method: "POST" });
      setSuccessMsg(`Stopped "${description}". ${res?.pending_removed ?? 0} pending transaction(s) removed.`);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleResume(item: RecurringTransaction) {
    try {
      await apiFetch(`/api/v1/recurring/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_active: true }),
      });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDelete(id: number) {
    setConfirmDeleteId(id);
  }

  async function doDelete(id: number) {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ pending_removed: number }>(`/api/v1/recurring/${id}`, { method: "DELETE" });
      setSuccessMsg(`Deleted. ${res?.pending_removed ?? 0} pending transaction(s) removed.`);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleGenerate() {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ generated: number }>("/api/v1/recurring/generate", { method: "POST" });
      setSuccessMsg(`Generated ${res?.generated ?? 0} transaction(s)`);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  const activeItems = items.filter((r) => r.is_active);
  const pausedItems = items.filter((r) => !r.is_active);

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Recurring Transactions</h1>
        <button onClick={handleGenerate} className={btnSecondary}>
          Generate Due
        </button>
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}
      {successMsg && <div className={`mb-6 ${successCls}`}>{successMsg}</div>}

      <p className="mb-6 text-sm text-text-muted">
        To create a recurring transaction, add a regular transaction from the{" "}
        <Link href="/transactions" className="text-accent hover:text-accent-hover">Transactions</Link>{" "}
        page or the Dashboard and check the &quot;Repeats&quot; option.
      </p>

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Active */}
          <div className={`${card} overflow-x-auto`}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Active ({activeItems.length})</h2>
            </div>
            <div className="divide-y divide-border-subtle">
              {activeItems.map((r) => (
                <div key={r.id} className="grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised">
                  <span className="col-span-3 text-sm text-text-primary">
                    {r.description}
                    {r.auto_settle && <span className="ml-1.5 rounded bg-success-dim px-1.5 py-0.5 text-[10px] font-medium text-success">auto</span>}
                  </span>
                  <span className="col-span-2 text-sm text-text-secondary">{r.account_name}</span>
                  <span className="col-span-2 text-sm text-text-secondary">{r.category_name}</span>
                  <span className="col-span-1 text-xs text-text-muted">{FREQ_LABELS[r.frequency] ?? r.frequency}</span>
                  <span className="col-span-1 text-sm tabular-nums text-text-secondary">{r.next_due_date}</span>
                  <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${r.type === "income" ? "text-success" : "text-danger"}`}>
                    {r.type === "income" ? "+" : "-"}{formatAmount(r.amount)}
                  </span>
                  <span className="col-span-2 flex justify-end gap-2">
                    <button onClick={() => handleStop(r)} className="text-xs text-text-muted hover:text-accent">Stop</button>
                    <button onClick={() => handleDelete(r.id)} className="text-xs text-text-muted hover:text-danger">Delete</button>
                  </span>
                </div>
              ))}
              {activeItems.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  No active recurring transactions.
                </div>
              )}
            </div>
          </div>

          {/* Paused */}
          {pausedItems.length > 0 && (
            <div className={`${card} overflow-x-auto`}>
              <div className={cardHeader}>
                <h2 className={cardTitle}>Paused ({pausedItems.length})</h2>
              </div>
              <div className="divide-y divide-border-subtle">
                {pausedItems.map((r) => (
                  <div key={r.id} className="grid grid-cols-12 items-center gap-4 px-6 py-3 opacity-50 transition-colors hover:bg-surface-raised">
                    <span className="col-span-3 text-sm text-text-primary">{r.description}</span>
                    <span className="col-span-2 text-sm text-text-secondary">{r.account_name}</span>
                    <span className="col-span-2 text-sm text-text-secondary">{r.category_name}</span>
                    <span className="col-span-1 text-xs text-text-muted">{FREQ_LABELS[r.frequency] ?? r.frequency}</span>
                    <span className="col-span-1 text-sm tabular-nums text-text-secondary">{r.next_due_date}</span>
                    <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${r.type === "income" ? "text-success" : "text-danger"}`}>
                      {r.type === "income" ? "+" : "-"}{formatAmount(r.amount)}
                    </span>
                    <span className="col-span-2 flex justify-end gap-2">
                      <button onClick={() => handleResume(r)} className="text-xs text-text-muted hover:text-accent">Resume</button>
                      <button onClick={() => handleDelete(r.id)} className="text-xs text-text-muted hover:text-danger">Delete</button>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      <ConfirmModal
        open={confirmStop !== null}
        title="Stop Recurring Transaction"
        message={confirmStop ? `Stop "${confirmStop.description}"?\n\nThis will deactivate the recurring schedule and delete any pending future transactions.\n\nSettled (past) transactions will NOT be affected.` : ""}
        confirmLabel="Stop"
        variant="warning"
        onConfirm={() => { if (confirmStop) { doStop(confirmStop.id, confirmStop.description); } setConfirmStop(null); }}
        onCancel={() => setConfirmStop(null)}
      />
      <ConfirmModal
        open={confirmDeleteId !== null}
        title="Delete Recurring Template"
        message="Permanently delete this recurring template?\n\nAny remaining pending future transactions will also be removed.\nSettled transactions are preserved."
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { if (confirmDeleteId !== null) { doDelete(confirmDeleteId); } setConfirmDeleteId(null); }}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </AppShell>
  );
}
