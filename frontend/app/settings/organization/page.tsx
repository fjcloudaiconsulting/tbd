"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { SettingsLayout } from "@/app/settings/page";
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
  const [billingCycleDay, setBillingCycleDay] = useState(user?.billing_cycle_day ?? 1);
  const [savingCycle, setSavingCycle] = useState(false);
  const [currentPeriod, setCurrentPeriod] = useState<{
    id: number;
    start_date: string;
    end_date: string | null;
  } | null>(null);
  const [closingPeriod, setClosingPeriod] = useState(false);

  const admin = user ? isAdmin(user) : false;

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
    }
  }, [admin, reload]);

  async function handleSaveCycle(e: FormEvent) {
    e.preventDefault();
    setSavingCycle(true);
    setError("");
    try {
      await apiFetch("/api/v1/settings/billing-cycle", {
        method: "PUT",
        body: JSON.stringify({ billing_cycle_day: billingCycleDay }),
      });
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
                    Current: {currentPeriod.start_date} — {currentPeriod.end_date ?? "open"}
                  </p>
                  <p className="text-xs text-text-muted">
                    {currentPeriod.end_date ? "Closed" : "Open — transactions are being recorded"}
                  </p>
                </div>
                {!currentPeriod.end_date && (
                  <button
                    onClick={handleClosePeriod}
                    disabled={closingPeriod}
                    className={btnPrimary}
                  >
                    {closingPeriod ? "Closing..." : "Close Period"}
                  </button>
                )}
              </div>
            )}

            <form onSubmit={handleSaveCycle} className="flex items-end gap-3">
              <div>
                <label className={label}>Billing cycle day</label>
                <input
                  type="number"
                  min={1}
                  max={28}
                  value={billingCycleDay}
                  onChange={(e) => setBillingCycleDay(Number(e.target.value))}
                  className={`${input} w-24`}
                />
              </div>
              <button type="submit" disabled={savingCycle} className={btnPrimary}>
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
            <form onSubmit={handleAdd} className="flex items-end gap-3">
              <div>
                <label className={label}>Key</label>
                <input value={key} onChange={(e) => setKey(e.target.value)} className={input} placeholder="key" />
              </div>
              <div>
                <label className={label}>Value</label>
                <input value={value} onChange={(e) => setValue(e.target.value)} className={input} placeholder="value" />
              </div>
              <button type="submit" className={btnPrimary}>Add</button>
            </form>

            {settings.length > 0 && (
              <table className="w-full text-sm">
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
