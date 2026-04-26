"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import {
  input,
  label,
  btnPrimary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  success as successCls,
} from "@/lib/styles";
import type { OrgSetting } from "@/lib/types";

export default function OrganizationSettingsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  const [settings, setSettings] = useState<OrgSetting[]>([]);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: string;
    variant: "warning" | "danger";
    action: () => void;
  } | null>(null);
  const [billingCycleDay, setBillingCycleDay] = useState<string>("");
  const [savingCycle, setSavingCycle] = useState(false);
  const [currentPeriod, setCurrentPeriod] = useState<{
    id: number;
    start_date: string;
    end_date: string | null;
  } | null>(null);
  const [closingPeriod, setClosingPeriod] = useState(false);

  const admin = user ? isAdmin(user) : false;

  const currentPeriodEndDisplay = (() => {
    if (!currentPeriod) return null;
    if (currentPeriod.end_date) return currentPeriod.end_date;
    const day = Number(billingCycleDay);
    if (!Number.isInteger(day) || day < 1 || day > 28) return null;
    const start = new Date(currentPeriod.start_date + "T00:00:00");
    const next = new Date(start.getFullYear(), start.getMonth() + 1, day);
    next.setDate(next.getDate() - 1);
    const yyyy = next.getFullYear();
    const mm = String(next.getMonth() + 1).padStart(2, "0");
    const dd = String(next.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  })();

  useEffect(() => {
    if (!loading && !admin) router.replace("/settings");
  }, [loading, admin, router]);

  const reload = useCallback(async () => {
    try {
      const data = await apiFetch<OrgSetting[]>("/api/v1/settings");
      setSettings(data);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    if (admin) {
      reload();
      apiFetch<{ id: number; start_date: string; end_date: string | null }>(
        "/api/v1/settings/billing-period"
      ).then(setCurrentPeriod).catch(() => {});
      apiFetch<{ billing_cycle_day: number }>("/api/v1/settings/billing-cycle")
        .then((r) => setBillingCycleDay(String(r.billing_cycle_day)))
        .catch(() => {});
    }
  }, [admin, reload]);

  async function handleSaveCycle(e: FormEvent) {
    e.preventDefault();
    setError("");
    const day = Number(billingCycleDay);
    if (!Number.isInteger(day) || day < 1 || day > 28) {
      setError("Billing cycle day must be a whole number between 1 and 28");
      return;
    }
    setSavingCycle(true);
    try {
      await apiFetch("/api/v1/settings/billing-cycle", {
        method: "PUT",
        body: JSON.stringify({ billing_cycle_day: day }),
      });
      const period = await apiFetch<{ id: number; start_date: string; end_date: string | null }>(
        "/api/v1/settings/billing-period"
      );
      setCurrentPeriod(period);
      setSuccessMsg("Billing cycle updated");
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setSavingCycle(false);
    }
  }

  function handleClosePeriod() {
    setConfirmAction({
      title: "Close Billing Period",
      message: `Close the current billing period starting ${currentPeriod?.start_date}?\nA new period will open automatically.`,
      variant: "warning",
      action: async () => {
        setClosingPeriod(true);
        try {
          await apiFetch("/api/v1/settings/billing-period/close", { method: "POST" });
          const p = await apiFetch<{ id: number; start_date: string; end_date: string | null }>(
            "/api/v1/settings/billing-period"
          );
          setCurrentPeriod(p);
          setSuccessMsg("Period closed");
          setTimeout(() => setSuccessMsg(""), 3000);
        } catch (err) {
          setError(extractErrorMessage(err));
        } finally {
          setClosingPeriod(false);
        }
      },
    });
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key, value }),
      });
      setKey("");
      setValue("");
      await reload();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleUpdate(settingKey: string) {
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key: settingKey, value: editingValue }),
      });
      setEditingKey(null);
      await reload();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleDelete(settingKey: string) {
    setConfirmAction({
      title: "Delete Setting",
      message: `Delete setting "${settingKey}"?`,
      variant: "danger",
      action: async () => {
        setError("");
        try {
          await apiFetch(`/api/v1/settings/${encodeURIComponent(settingKey)}`, {
            method: "DELETE",
          });
          await reload();
        } catch (err) {
          setError(extractErrorMessage(err));
        }
      },
    });
  }

  if (loading || !user || !admin) {
    return (
      <SettingsLayout activeTab="/settings/organization">
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      </SettingsLayout>
    );
  }

  return (
    <SettingsLayout activeTab="/settings/organization">
      {error && <p className={errorCls}>{error}</p>}
      {successMsg && <p className={successCls}>{successMsg}</p>}

      <div className="space-y-6">
        {/* Organization Name */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Organization</h2>
          </div>
          <div className="p-6">
            <p className="text-sm text-text-secondary">{user.org_name}</p>
          </div>
        </div>

        {/* Billing Period */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Billing Period</h2>
          </div>
          <div className="p-6 space-y-4">
            {currentPeriod && (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-text-primary">
                    Current: {currentPeriod.start_date}
                    {currentPeriodEndDisplay ? ` — ${currentPeriodEndDisplay}` : " — open"}
                  </p>
                  <p className="text-xs text-text-muted">
                    {currentPeriod.end_date ? "Closed" : "Open, transactions are being recorded"}
                  </p>
                </div>
                {!currentPeriod.end_date && (
                  <button
                    onClick={handleClosePeriod}
                    disabled={closingPeriod}
                    className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                  >
                    {closingPeriod ? "Closing..." : "Close Period"}
                  </button>
                )}
              </div>
            )}

            <form onSubmit={handleSaveCycle} className="flex flex-col gap-3 sm:flex-row sm:items-end sm:gap-3">
              <div>
                <label className={label}>Billing cycle day</label>
                <input
                  type="number"
                  min={1}
                  max={28}
                  value={billingCycleDay}
                  onChange={(e) => setBillingCycleDay(e.target.value)}
                  className={`${input} w-full sm:w-24`}
                />
              </div>
              <button type="submit" disabled={savingCycle} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                {savingCycle ? "Saving..." : "Save"}
              </button>
            </form>
          </div>
        </div>

        {/* Advanced Configuration */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Advanced Configuration</h2>
          </div>
          <div className="p-6 space-y-4">
            <form onSubmit={handleAdd} className="flex flex-col gap-3 sm:flex-row sm:items-end sm:gap-3">
              <div className="flex-1">
                <label className={label}>Key</label>
                <input value={key} onChange={(e) => setKey(e.target.value)} className={`${input} w-full`} placeholder="key" />
              </div>
              <div className="flex-1">
                <label className={label}>Value</label>
                <input value={value} onChange={(e) => setValue(e.target.value)} className={`${input} w-full`} placeholder="value" />
              </div>
              <button type="submit" className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>Add</button>
            </form>

            {settings.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs uppercase text-text-muted">
                      <th className="pb-2">Key</th>
                      <th className="pb-2">Value</th>
                      <th className="pb-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {settings.map((s) => (
                      <tr key={s.key} className="border-b border-border">
                        <td className="py-2 text-text-primary">{s.key}</td>
                        <td className="py-2">
                          {editingKey === s.key ? (
                            <input
                              value={editingValue}
                              onChange={(e) => setEditingValue(e.target.value)}
                              onKeyDown={(e) => e.key === "Enter" && handleUpdate(s.key)}
                              className={`${input} w-48`}
                              autoFocus
                            />
                          ) : (
                            <span className="text-text-secondary">{s.value}</span>
                          )}
                        </td>
                        <td className="py-2 text-right space-x-2">
                          {editingKey === s.key ? (
                            <>
                              <button onClick={() => handleUpdate(s.key)} className="text-xs text-accent hover:underline">Save</button>
                              <button onClick={() => setEditingKey(null)} className="text-xs text-text-muted hover:underline">Cancel</button>
                            </>
                          ) : (
                            <>
                              <button onClick={() => { setEditingKey(s.key); setEditingValue(s.value); }} className="text-xs text-accent hover:underline">Edit</button>
                              <button onClick={() => handleDelete(s.key)} className="text-xs text-danger hover:underline">Delete</button>
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      <ConfirmModal
        open={!!confirmAction}
        title={confirmAction?.title ?? ""}
        message={confirmAction?.message ?? ""}
        variant={confirmAction?.variant ?? "warning"}
        onConfirm={() => {
          confirmAction?.action();
          setConfirmAction(null);
        }}
        onCancel={() => setConfirmAction(null)}
      />
    </SettingsLayout>
  );
}
