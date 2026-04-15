"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, success as successCls, pageTitle } from "@/lib/styles";
import type { OrgSetting } from "@/lib/types";

export default function SettingsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [settings, setSettings] = useState<OrgSetting[]>([]);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");

  // Confirm modal
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: string;
    variant: "warning" | "danger";
    action: () => void;
  } | null>(null);

  // Billing
  const [billingCycleDay, setBillingCycleDay] = useState(user?.billing_cycle_day ?? 1);
  const [savingCycle, setSavingCycle] = useState(false);
  const [currentPeriod, setCurrentPeriod] = useState<{ id: number; start_date: string; end_date: string | null } | null>(null);
  const [closingPeriod, setClosingPeriod] = useState(false);

  const admin = user ? isAdmin(user) : false;

  useEffect(() => {
    if (!loading && !admin) router.replace("/dashboard");
  }, [loading, admin, router]);

  const reload = useCallback(async () => {
    try {
      const data = await apiFetch<OrgSetting[]>("/api/v1/settings");
      setSettings(data ?? []);
    } catch { /* May 403 if not admin */ }
  }, []);

  useEffect(() => {
    if (admin) reload();
  }, [admin, reload]);

  useEffect(() => {
    if (user?.billing_cycle_day) setBillingCycleDay(user.billing_cycle_day);
  }, [user]);

  // Load current billing period
  useEffect(() => {
    if (admin) {
      apiFetch<{ id: number; start_date: string; end_date: string | null }>("/api/v1/settings/billing-period")
        .then((p) => { if (p) setCurrentPeriod(p); })
        .catch(() => {});
    }
  }, [admin]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/settings", { method: "PUT", body: JSON.stringify({ key, value }) });
      setKey(""); setValue("");
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleUpdate(settingKey: string) {
    setError("");
    try {
      await apiFetch("/api/v1/settings", { method: "PUT", body: JSON.stringify({ key: settingKey, value: editingValue }) });
      setEditingKey(null);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDelete(settingKey: string) {
    setConfirmAction({
      title: "Delete Setting",
      message: `Delete setting "${settingKey}"?`,
      variant: "danger",
      action: async () => {
        setError("");
        try {
          await apiFetch(`/api/v1/settings/${encodeURIComponent(settingKey)}`, { method: "DELETE" });
          await reload();
        } catch (err) { setError(extractErrorMessage(err)); }
      },
    });
  }

  if (loading || !admin) {
    return <AppShell>{loading && <Spinner />}</AppShell>;
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Organization Settings</h1>

      <div className="max-w-2xl space-y-6">
        <div className={`${card} p-6`}>
          <h2 className={`mb-2 ${cardTitle}`}>Organization</h2>
          <p className="text-sm text-text-primary">{user?.org_name}</p>
        </div>

        {/* Billing Period */}
        <div className={`${card} p-6`}>
          <h2 className={`mb-4 ${cardTitle}`}>Billing Period</h2>
          {successMsg && <div className={`mb-4 ${successCls}`}>{successMsg}</div>}

          {currentPeriod && (
            <div className="mb-4 rounded-md bg-surface-raised px-4 py-3">
              <p className="text-sm text-text-primary">
                Current period: <span className="font-medium">{currentPeriod.start_date}</span>
                {currentPeriod.end_date
                  ? <> — <span className="font-medium">{currentPeriod.end_date}</span></>
                  : <span className="ml-1 text-success text-xs font-medium">open</span>
                }
              </p>
            </div>
          )}

          <p className="mb-4 text-xs text-text-muted">
            Close the current period when you receive your salary. The next period starts the following day.
            Past settled transactions remain in their original period.
          </p>

          <div className="flex flex-wrap items-end gap-4">
            <button
              disabled={closingPeriod}
              onClick={() => {
                setConfirmAction({
                  title: "Close Billing Period",
                  message: "Close the current billing period?\n\nA new period will start tomorrow. Budgets for the new period will need to be set.",
                  variant: "warning",
                  action: async () => {
                    setClosingPeriod(true); setError(""); setSuccessMsg("");
                    try {
                      const newP = await apiFetch<{ id: number; start_date: string; end_date: string | null }>("/api/v1/settings/billing-period/close", { method: "POST" });
                      if (newP) setCurrentPeriod(newP);
                      setSuccessMsg("Period closed. New period started.");
                    } catch (err) { setError(extractErrorMessage(err)); }
                    finally { setClosingPeriod(false); }
                  },
                });
              }}
              className={btnPrimary}
            >
              {closingPeriod ? "Closing..." : "Close Current Period"}
            </button>

            <div className="border-l border-border pl-4">
              <label htmlFor="cycle-day" className="text-xs text-text-muted mb-1 block">Default cycle hint day (for new periods)</label>
              <div className="flex items-center gap-2">
                <input
                  id="cycle-day"
                  type="number"
                  min={1}
                  max={28}
                  value={billingCycleDay}
                  onChange={(e) => setBillingCycleDay(Number(e.target.value))}
                  className={`w-20 text-sm ${input}`}
                />
                <button
                  disabled={savingCycle}
                  onClick={async () => {
                    setSavingCycle(true); setError(""); setSuccessMsg("");
                    try {
                      await apiFetch("/api/v1/settings/billing-cycle", {
                        method: "PUT",
                        body: JSON.stringify({ billing_cycle_day: billingCycleDay }),
                      });
                      setSuccessMsg("Default cycle day updated.");
                    } catch (err) { setError(extractErrorMessage(err)); }
                    finally { setSavingCycle(false); }
                  }}
                  className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-surface-raised"
                >
                  {savingCycle ? "..." : "Save"}
                </button>
              </div>
            </div>
          </div>
        </div>

        <details className={card}>
          <summary className={`cursor-pointer ${cardHeader}`}>
            <h2 className={`inline ${cardTitle}`}>Advanced Configuration</h2>
            <p className="mt-1 text-xs text-text-muted">Custom key-value settings for developers. Most users don't need this.</p>
          </summary>
          <div className="p-6">
            {error && <div className={`mb-5 ${errorCls}`}>{error}</div>}

            <form onSubmit={handleAdd} className="mb-5 flex gap-2">
              <div className="w-40">
                <label htmlFor="setting-key" className="sr-only">Setting key</label>
                <input id="setting-key" type="text" required placeholder="Key" value={key} onChange={(e) => setKey(e.target.value)} className={input} />
              </div>
              <div className="flex-1">
                <label htmlFor="setting-value" className="sr-only">Setting value</label>
                <input id="setting-value" type="text" required placeholder="Value" value={value} onChange={(e) => setValue(e.target.value)} className={input} />
              </div>
              <button type="submit" className={btnPrimary}>Add</button>
            </form>

            <div className="space-y-1">
              {settings.map((s) => (
                <div key={s.key} className="flex items-center justify-between rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised">
                  {editingKey === s.key ? (
                    <div className="flex flex-1 gap-2">
                      <span className="w-40 py-1 text-sm font-medium text-text-secondary">{s.key}</span>
                      <label htmlFor={`edit-setting-${s.key}`} className="sr-only">Edit value for {s.key}</label>
                      <input id={`edit-setting-${s.key}`} type="text" value={editingValue} onChange={(e) => setEditingValue(e.target.value)} className={`flex-1 ${input}`} autoFocus
                        onKeyDown={(e) => { if (e.key === "Enter") handleUpdate(s.key); if (e.key === "Escape") setEditingKey(null); }} />
                      <button onClick={() => handleUpdate(s.key)} className="text-sm text-accent hover:text-accent-hover">Save</button>
                      <button onClick={() => setEditingKey(null)} className="text-sm text-text-muted hover:text-text-secondary">Cancel</button>
                    </div>
                  ) : (
                    <>
                      <div>
                        <span className="text-sm font-medium text-text-secondary">{s.key}</span>
                        <span className="ml-3 text-sm text-text-muted">{s.value}</span>
                      </div>
                      <div className="flex gap-3">
                        <button onClick={() => { setEditingKey(s.key); setEditingValue(s.value); }} aria-label={`Edit ${s.key}`} className="text-xs text-text-muted hover:text-accent">Edit</button>
                        <button onClick={() => handleDelete(s.key)} aria-label={`Delete ${s.key}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                      </div>
                    </>
                  )}
                </div>
              ))}
              {settings.length === 0 && <p className="py-4 text-center text-sm text-text-muted">No settings configured yet.</p>}
            </div>
          </div>
        </details>
      </div>
      <ConfirmModal
        open={confirmAction !== null}
        title={confirmAction?.title ?? ""}
        message={confirmAction?.message ?? ""}
        confirmLabel="Confirm"
        variant={confirmAction?.variant ?? "default"}
        onConfirm={() => { confirmAction?.action(); setConfirmAction(null); }}
        onCancel={() => setConfirmAction(null)}
      />
    </AppShell>
  );
}
