"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { mutate } from "swr";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { projectedPeriodEnd } from "@/lib/format";
import { isAdmin } from "@/lib/auth";
import MembersSection from "@/components/settings/MembersSection";
import SmartRulesSection from "@/components/settings/SmartRulesSection";
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
  // Initial value falls back to AuthContext.user.billing_cycle_day so a slow or
  // failed GET /billing-cycle never leaves the field unusably blank.
  const [billingCycleDay, setBillingCycleDay] = useState<string>(
    user?.billing_cycle_day != null ? String(user.billing_cycle_day) : ""
  );
  // True once the admin has typed; the mount-time GET response is dropped if
  // this is set, so a slow response can't overwrite an in-progress edit or
  // resurrect a stale value right after a fast Save.
  const userEditedCycleDayRef = useRef(false);
  const [savingCycle, setSavingCycle] = useState(false);
  const [currentPeriod, setCurrentPeriod] = useState<{
    id: number;
    start_date: string;
    end_date: string | null;
  } | null>(null);
  const [closingPeriod, setClosingPeriod] = useState(false);

  // L3.1 Danger Zone — owner-only data reset.
  // Compare user.role exactly; do NOT use isOwner() from @/lib/auth,
  // which treats is_superadmin as owner — the backend rejects
  // superadmin tenant bypass, so the UI must mirror that.
  const [resetPhrase, setResetPhrase] = useState("");
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState("");

  const admin = user ? isAdmin(user) : false;
  const isOrgOwner = user?.role === "owner";
  const orgName = user?.org_name ?? "";
  const expectedResetPhrase = `RESET ${orgName}`;
  const resetPhraseMatches = resetPhrase.trim() === expectedResetPhrase;

  const currentPeriodEndDisplay = currentPeriod
    ? currentPeriod.end_date ?? projectedPeriodEnd(currentPeriod.start_date, Number(billingCycleDay))
    : null;

  useEffect(() => {
    if (!loading && !admin) router.replace("/settings");
  }, [loading, admin, router]);

  // AuthProvider hydrates `user` asynchronously, so the state initializer
  // typically locks the field at "" before user.billing_cycle_day exists.
  // Once it lands, seed the field — but only if the admin hasn't started
  // editing and an authoritative GET response hasn't already filled the
  // field. This is the failed-GET fallback the initializer alone can't
  // provide.
  useEffect(() => {
    if (user?.billing_cycle_day == null) return;
    if (userEditedCycleDayRef.current) return;
    setBillingCycleDay((current) =>
      current === "" ? String(user.billing_cycle_day) : current
    );
  }, [user?.billing_cycle_day]);

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
        .then((r) => {
          if (!userEditedCycleDayRef.current) {
            setBillingCycleDay(String(r.billing_cycle_day));
          }
        })
        .catch(() => {
          // Swallow: the AuthContext-seeding effect above leaves a usable
          // (possibly stale) value in place when GET fails. If user is also
          // unavailable, the field stays empty and client-side validation
          // will guide the admin on Save.
        });
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
      // Server now matches local state — clear the dirty flag so a future GET
      // (e.g., on revisit) can re-sync without being treated as a stale overwrite.
      userEditedCycleDayRef.current = false;
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
                  onChange={(e) => {
                    userEditedCycleDayRef.current = true;
                    setBillingCycleDay(e.target.value);
                  }}
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

      {user && (
        <div className="mt-6 space-y-6">
          <MembersSection
            currentUserId={user.id}
            currentRole={user.role as "owner" | "admin" | "member"}
          />
          <SmartRulesSection />
        </div>
      )}

      {isOrgOwner && (
        <div className="mt-6">
          <section className={`${card} border-danger/40`}>
            <div className={cardHeader}>
              <h2 className={`${cardTitle} text-danger`}>Danger zone</h2>
            </div>
            <div className="px-6 py-5 space-y-3">
              <p className="text-sm text-text-secondary">
                Resetting wipes <strong>transactions, accounts, account types, categories, smart rules, budgets, forecast plans, recurring transactions, and billing periods</strong>. Your organization, members, subscription, settings, feature overrides, and pending invitations are preserved. The action cannot be undone.
              </p>
              <p className="text-sm text-text-secondary">
                Type <code className="rounded bg-surface-raised px-1.5 py-0.5 font-mono text-text-primary">RESET {orgName}</code> to confirm:
              </p>
              <input
                type="text"
                aria-label="Confirm reset phrase"
                value={resetPhrase}
                onChange={(e) => setResetPhrase(e.target.value)}
                placeholder={expectedResetPhrase}
                className={`${input} max-w-md`}
              />
              {resetError && <p className="text-sm text-danger">{resetError}</p>}
              <div>
                <button
                  type="button"
                  onClick={async () => {
                    if (!resetPhraseMatches) return;
                    setResetError("");
                    setResetting(true);
                    try {
                      await apiFetch("/api/v1/orgs/data/reset", {
                        method: "POST",
                        body: JSON.stringify({ confirm_phrase: resetPhrase.trim() }),
                      });
                      // Clear every SWR cache key without revalidating —
                      // the reset wiped accounts/categories/etc. on the
                      // server, so any cached value in this client session
                      // (e.g. /import's account+category lists) would
                      // briefly show deleted rows. Skipping revalidation
                      // here is safe because we navigate away immediately
                      // and the destination's hooks will refetch fresh.
                      await mutate(() => true, undefined, { revalidate: false });
                      router.push("/dashboard?reset=1");
                    } catch (err) {
                      setResetError(extractErrorMessage(err, "Reset failed"));
                      setResetting(false);
                    }
                  }}
                  disabled={!resetPhraseMatches || resetting}
                  className="rounded-md bg-danger px-4 py-2 text-sm font-medium text-white hover:bg-danger/90 disabled:opacity-50"
                >
                  {resetting ? "Resetting…" : "Reset my data"}
                </button>
              </div>
            </div>
          </section>
        </div>
      )}

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
