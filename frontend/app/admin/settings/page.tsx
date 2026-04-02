"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
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

  // Billing cycle
  const [billingCycleDay, setBillingCycleDay] = useState(user?.billing_cycle_day ?? 1);
  const [savingCycle, setSavingCycle] = useState(false);

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

  // Sync billing cycle from user when available
  useEffect(() => {
    if (user?.billing_cycle_day) setBillingCycleDay(user.billing_cycle_day);
  }, [user]);

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
    if (!confirm(`Delete setting "${settingKey}"?`)) return;
    setError("");
    try {
      await apiFetch(`/api/v1/settings/${encodeURIComponent(settingKey)}`, { method: "DELETE" });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
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

        {/* Billing Cycle */}
        <div className={`${card} p-6`}>
          <h2 className={`mb-4 ${cardTitle}`}>Billing Cycle</h2>
          <p className="mb-4 text-xs text-text-muted">
            Set the day of the month when your billing cycle starts. This affects how the dashboard
            and budgets define &quot;current month&quot; (e.g., day 15 means 15th to 14th).
          </p>
          {successMsg && <div className={`mb-4 ${successCls}`}>{successMsg}</div>}
          <div className="flex items-end gap-3">
            <div>
              <label htmlFor="cycle-day" className={label}>Cycle Start Day (1-28)</label>
              <input
                id="cycle-day"
                type="number"
                min={1}
                max={28}
                value={billingCycleDay}
                onChange={(e) => setBillingCycleDay(Number(e.target.value))}
                className={`w-24 ${input}`}
              />
            </div>
            <button
              disabled={savingCycle}
              onClick={async () => {
                setSavingCycle(true); setError(""); setSuccessMsg("");
                try {
                  await apiFetch("/api/v1/settings/billing-cycle", {
                    method: "PUT",
                    body: JSON.stringify({ billing_cycle_day: billingCycleDay }),
                  });
                  setSuccessMsg("Billing cycle updated. Refresh to see changes on the dashboard.");
                } catch (err) { setError(extractErrorMessage(err)); }
                finally { setSavingCycle(false); }
              }}
              className={btnPrimary}
            >
              {savingCycle ? "Saving..." : "Save"}
            </button>
          </div>
        </div>

        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Configuration</h2>
            <p className="mt-1 text-xs text-text-muted">Runtime settings persisted in the database.</p>
          </div>
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
        </div>
      </div>
    </AppShell>
  );
}
