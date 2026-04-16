"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isOwner } from "@/lib/auth";
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  success as successCls,
} from "@/lib/styles";
import type { Plan, SubscriptionDetail } from "@/lib/types";

export default function BillingPage() {
  const { user, loading, refreshMe } = useAuth();
  const router = useRouter();
  const [subscription, setSubscription] = useState<SubscriptionDetail | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loadingSub, setLoadingSub] = useState(true);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: string;
    variant: "warning" | "danger";
    action: () => void;
  } | null>(null);

  const owner = user ? isOwner(user) : false;

  useEffect(() => {
    if (!loading && !owner) router.replace("/settings");
  }, [loading, owner, router]);

  useEffect(() => {
    if (!owner) return;
    Promise.all([
      apiFetch<SubscriptionDetail>("/api/v1/subscriptions"),
      apiFetch<Plan[]>("/api/v1/plans"),
    ])
      .then(([sub, p]) => {
        setSubscription(sub);
        setPlans(p);
      })
      .catch((err) => setError(extractErrorMessage(err)))
      .finally(() => setLoadingSub(false));
  }, [owner]);

  async function handleChangePlan(planSlug: string, interval: string) {
    setError("");
    try {
      const sub = await apiFetch<SubscriptionDetail>("/api/v1/subscriptions/plan", {
        method: "PUT",
        body: JSON.stringify({ plan_slug: planSlug, billing_interval: interval }),
      });
      setSubscription(sub);
      setSuccessMsg("Plan updated");
      setTimeout(() => setSuccessMsg(""), 3000);
      await refreshMe();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  function handleCancel() {
    setConfirmAction({
      title: "Cancel Subscription",
      message: "Your access will continue until the end of your current billing period. Are you sure?",
      variant: "danger",
      action: async () => {
        setError("");
        try {
          const sub = await apiFetch<SubscriptionDetail>("/api/v1/subscriptions/cancel", {
            method: "POST",
          });
          setSubscription(sub);
          setSuccessMsg("Subscription canceled");
          setTimeout(() => setSuccessMsg(""), 3000);
          await refreshMe();
        } catch (err) {
          setError(extractErrorMessage(err));
        }
      },
    });
  }

  if (loading || !user || !owner || loadingSub) {
    return (
      <SettingsLayout activeTab="/settings/billing">
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      </SettingsLayout>
    );
  }

  const sub = subscription;
  const currentPlan = sub?.plan;
  const isTrialing = sub?.status === "trialing";
  const isCanceled = sub?.status === "canceled";

  let trialDaysLeft = 0;
  if (isTrialing && sub?.trial_end) {
    const endMs = Date.parse(sub.trial_end + "T23:59:59Z");
    trialDaysLeft = Math.max(0, Math.floor((endMs - Date.now()) / 86_400_000));
  }

  return (
    <SettingsLayout activeTab="/settings/billing">
      {error && <p className={`${errorCls} mb-4`}>{error}</p>}
      {successMsg && <p className={`${successCls} mb-4`}>{successMsg}</p>}

      <div className="space-y-6">
        {/* Beta Notice */}
        <div className="rounded-lg border border-accent/30 bg-accent/5 p-4">
          <p className="text-sm text-accent">
            PFV2 is in beta — no charges will be applied. Subscription management is fully functional for testing.
          </p>
        </div>

        {/* Current Plan */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Current Plan</h2>
          </div>
          <div className="p-6">
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-lg font-semibold text-text-primary">
                    {currentPlan?.name ?? "None"}
                  </span>
                  {isTrialing && (
                    <span className="rounded-full bg-accent/20 px-2 py-0.5 text-[11px] font-medium text-accent">
                      TRIAL
                    </span>
                  )}
                  {isCanceled && (
                    <span className="rounded-full bg-danger-dim px-2 py-0.5 text-[11px] font-medium text-danger">
                      CANCELED
                    </span>
                  )}
                </div>
                {isTrialing && (
                  <p className="text-sm text-text-muted">
                    Trial ends {sub?.trial_end} — {trialDaysLeft} day{trialDaysLeft !== 1 ? "s" : ""} remaining
                  </p>
                )}
                {isCanceled && sub?.current_period_end && (
                  <p className="text-sm text-text-muted">
                    Access until {sub.current_period_end}
                  </p>
                )}
              </div>
              <div className="text-right">
                <div className="text-2xl font-bold text-text-primary">
                  {currentPlan && currentPlan.price_monthly > 0 ? (
                    <>
                      €{sub?.billing_interval === "yearly"
                        ? (currentPlan.price_yearly / 12).toFixed(2)
                        : currentPlan.price_monthly.toFixed(2)}
                      <span className="text-sm font-normal text-text-muted">/mo</span>
                    </>
                  ) : (
                    <>€0<span className="text-sm font-normal text-text-muted">/mo</span></>
                  )}
                </div>
                <p className="text-[11px] text-text-muted">No charge during beta</p>
              </div>
            </div>

            {currentPlan && (
              <div className="mt-6 grid grid-cols-3 gap-4 border-t border-border pt-4">
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-text-muted">Users</p>
                  <p className="text-sm font-medium text-text-primary">
                    {currentPlan.max_users ?? "Unlimited"}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-text-muted">Data Retention</p>
                  <p className="text-sm font-medium text-text-primary">
                    {currentPlan.retention_days ? `${currentPlan.retention_days} days` : "Unlimited"}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-text-muted">AI Features</p>
                  <p className="text-sm font-medium text-text-primary">
                    {currentPlan.ai_smart_plan_enabled
                      ? "Full Access"
                      : currentPlan.ai_budget_enabled
                        ? "Budget Only"
                        : "None"}
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Available Plans */}
        <div className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Available Plans</h2>
          </div>
          <div className="p-6">
            <div className="grid gap-4 sm:grid-cols-2">
              {plans.map((plan) => {
                const isCurrent = currentPlan?.slug === plan.slug;
                return (
                  <div
                    key={plan.id}
                    className={`rounded-lg border p-5 ${
                      isCurrent
                        ? "border-accent bg-accent/5"
                        : "border-border"
                    }`}
                  >
                    <div className="mb-3">
                      <h3 className="text-base font-semibold text-text-primary">{plan.name}</h3>
                      <p className="text-xs text-text-muted">{plan.description}</p>
                    </div>
                    <div className="mb-4">
                      <span className="text-xl font-bold text-text-primary">
                        €{plan.price_monthly.toFixed(2)}
                      </span>
                      <span className="text-sm text-text-muted">/mo</span>
                      {plan.price_yearly > 0 && (
                        <p className="text-[11px] text-text-muted">
                          or €{plan.price_yearly.toFixed(2)}/yr (save 20%)
                        </p>
                      )}
                    </div>
                    <ul className="mb-4 space-y-1 text-xs text-text-secondary">
                      <li>
                        {plan.max_users ? `Up to ${plan.max_users} user${plan.max_users > 1 ? "s" : ""}` : "Unlimited users"}
                      </li>
                      <li>
                        {plan.retention_days ? `${plan.retention_days}-day data retention` : "Unlimited retention"}
                      </li>
                      <li>
                        {plan.ai_smart_plan_enabled
                          ? "All AI features"
                          : plan.ai_budget_enabled
                            ? "AI budget suggestions"
                            : "No AI features"}
                      </li>
                    </ul>
                    {isCurrent ? (
                      <span className="inline-block rounded-md bg-accent/20 px-3 py-1.5 text-xs font-medium text-accent">
                        Current Plan
                      </span>
                    ) : (
                      <button
                        onClick={() => handleChangePlan(plan.slug, sub?.billing_interval ?? "monthly")}
                        className={plan.price_monthly > (currentPlan?.price_monthly ?? 0) ? btnPrimary : btnSecondary}
                      >
                        {plan.price_monthly > (currentPlan?.price_monthly ?? 0) ? "Upgrade" : "Downgrade"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Cancel */}
        {sub && sub.status !== "canceled" && currentPlan && currentPlan.price_monthly > 0 && (
          <div className="text-right">
            <button onClick={handleCancel} className="text-xs text-text-muted hover:text-danger">
              Cancel subscription
            </button>
          </div>
        )}
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
