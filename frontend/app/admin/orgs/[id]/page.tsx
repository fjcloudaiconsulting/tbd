"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import ChangePlanModal from "@/components/admin/ChangePlanModal";
import FeatureOverridesCard from "@/components/admin/FeatureOverridesCard";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  btnPrimary,
  btnSecondary,
  btnDangerSolid,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label,
  pageTitle,
} from "@/lib/styles";

type Subscription = {
  status: string;
  plan_id: number;
  plan_slug: string | null;
  trial_start: string | null;
  trial_end: string | null;
  current_period_start: string | null;
  current_period_end: string | null;
  created_at: string | null;
  updated_at: string | null;
};

type Member = {
  id: number;
  username: string;
  email: string;
  role: string;
  is_active: boolean;
  email_verified: boolean;
  created_at: string | null;
  // Optional in the legacy /api/v1/admin/orgs/{id} payload; the
  // dedicated /members endpoint includes it. Default `false` if
  // absent so existing call sites still narrow correctly.
  is_superadmin?: boolean;
};

const ROLE_OPTIONS = ["owner", "admin", "member"] as const;

type OrgDetail = {
  id: number;
  name: string;
  billing_cycle_day: number;
  created_at: string | null;
  subscription: Subscription | Record<string, never>;
  members: Member[];
  counts: { transactions: number; accounts: number; budgets: number; forecast_plans: number };
};

type PlanOption = {
  id: number;
  slug: string;
  name: string;
};

const STATUS_OPTIONS = ["trialing", "active", "past_due", "canceled"];

export default function AdminOrgDetailPage() {
  const params = useParams();
  const orgId = Number(params?.id);
  const { user, loading } = useAuth();
  const router = useRouter();
  const [detail, setDetail] = useState<OrgDetail | null>(null);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  // Subscription edit state
  const [subStatus, setSubStatus] = useState<string>("");
  const [subTrialEnd, setSubTrialEnd] = useState<string>("");
  const [subPeriodEnd, setSubPeriodEnd] = useState<string>("");
  const [plans, setPlans] = useState<PlanOption[]>([]);

  // Change-plan modal
  const [showChangePlan, setShowChangePlan] = useState(false);

  // Delete confirmation
  const [confirmName, setConfirmName] = useState("");
  const [deleting, setDeleting] = useState(false);

  // Member management state (L4.4 slice).
  //
  // The "Remove" affordance was retired on 2026-05-14: the underlying
  // DELETE endpoint shared its effect with PATCH ``is_active=False``
  // (soft-deactivate) but emitted a misleading
  // ``admin.org.member.removed`` audit event. Both flows now route
  // through PATCH and surface the same confirm dialog so the audit
  // log and the UI agree on what just happened.
  const [memberBusyId, setMemberBusyId] = useState<number | null>(null);
  const [memberError, setMemberError] = useState("");
  const [deactivateTarget, setDeactivateTarget] = useState<Member | null>(null);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "orgs.manage")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  async function refresh() {
    try {
      const [d, planList, members] = await Promise.all([
        apiFetch<OrgDetail>(`/api/v1/admin/orgs/${orgId}`),
        apiFetch<PlanOption[]>("/api/v1/plans"),
        // The dedicated members endpoint carries the superset shape
        // (is_superadmin in particular) so admin affordances render
        // correctly. Falls back gracefully to d.members if it errors
        // or returns a non-array shape (older API / mocks).
        apiFetch<Member[]>(`/api/v1/admin/orgs/${orgId}/members`).catch(
          () => null,
        ),
      ]);
      const useMembers = Array.isArray(members) ? members : d.members;
      setDetail({ ...d, members: useMembers });
      setPlans(planList ?? []);
      const sub = d.subscription as Subscription;
      setSubStatus(sub.status ?? "");
      setSubTrialEnd(sub.trial_end ?? "");
      setSubPeriodEnd(sub.current_period_end ?? "");
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load"));
    }
  }

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "orgs.manage") || !orgId) return;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, user, orgId]);

  async function saveSubscription(e: FormEvent) {
    e.preventDefault();
    setError("");
    setInfo("");
    try {
      const body: Record<string, unknown> = {};
      const sub = detail?.subscription as Subscription;
      if (subStatus && subStatus !== sub.status) body.status = subStatus;
      if (subTrialEnd && subTrialEnd !== sub.trial_end) body.trial_end = subTrialEnd;
      if (subPeriodEnd && subPeriodEnd !== sub.current_period_end) {
        body.current_period_end = subPeriodEnd;
      }
      if (Object.keys(body).length === 0) {
        setInfo("No changes.");
        return;
      }
      await apiFetch(`/api/v1/admin/orgs/${orgId}/subscription`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      setInfo("Subscription updated.");
      await refresh();
    } catch (err) {
      setError(extractErrorMessage(err, "Update failed"));
    }
  }

  async function patchMember(
    member: Member,
    body: { role?: string; is_active?: boolean },
  ) {
    setMemberError("");
    setMemberBusyId(member.id);
    try {
      await apiFetch(
        `/api/v1/admin/orgs/${orgId}/members/${member.id}`,
        { method: "PATCH", body: JSON.stringify(body) },
      );
      await refresh();
    } catch (err) {
      setMemberError(extractErrorMessage(err, "Update failed"));
    } finally {
      setMemberBusyId(null);
    }
  }

  async function confirmDeactivateMember() {
    if (!deactivateTarget) return;
    const member = deactivateTarget;
    setMemberError("");
    setMemberBusyId(member.id);
    try {
      await apiFetch(
        `/api/v1/admin/orgs/${orgId}/members/${member.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({ is_active: false }),
        },
      );
      setDeactivateTarget(null);
      await refresh();
    } catch (err) {
      setMemberError(extractErrorMessage(err, "Deactivate failed"));
    } finally {
      setMemberBusyId(null);
    }
  }

  async function handleDelete() {
    if (!detail) return;
    setError("");
    setDeleting(true);
    try {
      await apiFetch(`/api/v1/admin/orgs/${orgId}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm_name: confirmName }),
      });
      router.push("/admin/orgs");
    } catch (err) {
      setError(extractErrorMessage(err, "Delete failed"));
      setDeleting(false);
    }
  }

  if (loading || !user || !hasPlatformPermission(user, "orgs.manage")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  if (!detail) {
    return (
      <AppShell>
        {error ? (
          <div className={errorCls} role="alert">{error}</div>
        ) : (
          <Spinner />
        )}
      </AppShell>
    );
  }

  const sub = detail.subscription as Subscription;
  const confirmMatches = confirmName === detail.name;
  const currentPlanName =
    plans.find((p) => p.slug === sub.plan_slug)?.name ?? sub.plan_slug ?? "—";

  return (
    <AppShell>
      <div className="mb-4 flex items-center gap-2 text-sm text-text-muted">
        <Link href="/admin/orgs" className="text-accent hover:text-accent-hover">
          Organizations
        </Link>
        <span>/</span>
        <span className="text-text-secondary">{detail.name}</span>
      </div>
      <h1 className={pageTitle}>{detail.name}</h1>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">{error}</div>
      )}
      {info && (
        <div className="mb-4 rounded-md border border-border bg-surface-raised px-4 py-3 text-sm text-text-secondary">
          {info}
        </div>
      )}

      {/* Subscription card */}
      <section className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Subscription</h2>
        </div>
        <div className="px-6 py-5 space-y-4">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <dt className="text-text-muted">Plan</dt>
            <dd className="text-text-primary">{sub.plan_slug ?? "—"}</dd>
            <dt className="text-text-muted">Created</dt>
            <dd className="text-text-secondary">{sub.created_at?.slice(0, 10) ?? "—"}</dd>
            <dt className="text-text-muted">Trial start → end</dt>
            <dd className="text-text-secondary">
              {(sub.trial_start ?? "—")} → {(sub.trial_end ?? "—")}
            </dd>
            <dt className="text-text-muted">Period start → end</dt>
            <dd className="text-text-secondary">
              {(sub.current_period_start ?? "—")} → {(sub.current_period_end ?? "—")}
            </dd>
          </dl>

          <div className="flex items-center gap-3">
            <span className="text-sm text-text-secondary">
              Plan: <strong className="text-text-primary">{currentPlanName}</strong>
            </span>
            <button
              type="button"
              onClick={() => setShowChangePlan(true)}
              className={btnSecondary}
            >
              Change plan
            </button>
          </div>

          <form onSubmit={saveSubscription} className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 lg:items-end">
            <div>
              <label htmlFor="sub-status" className={label}>Status</label>
              <select
                id="sub-status"
                value={subStatus}
                onChange={(e) => setSubStatus(e.target.value)}
                className={input}
              >
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="sub-trial-end" className={label}>Trial end</label>
              <input
                id="sub-trial-end"
                type="date"
                value={subTrialEnd}
                onChange={(e) => setSubTrialEnd(e.target.value)}
                className={input}
              />
            </div>
            <div>
              <label htmlFor="sub-period-end" className={label}>Period end</label>
              <input
                id="sub-period-end"
                type="date"
                value={subPeriodEnd}
                onChange={(e) => setSubPeriodEnd(e.target.value)}
                className={input}
              />
            </div>
            <div className="lg:col-span-3">
              <button type="submit" className={btnPrimary}>Save</button>
            </div>
          </form>
        </div>
      </section>

      {showChangePlan && (
        <ChangePlanModal
          orgId={detail.id}
          currentPlanSlug={sub.plan_slug ?? ""}
          onClose={() => setShowChangePlan(false)}
          onChanged={async () => { await refresh(); }}
        />
      )}

      {/* Feature overrides card */}
      <FeatureOverridesCard orgId={detail.id} />

      {/* Members card */}
      <section className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Members ({detail.members.length})</h2>
        </div>
        {memberError && (
          <div className="px-6 pt-4">
            <div className={errorCls} role="alert">{memberError}</div>
          </div>
        )}
        <div className="overflow-x-auto px-6 py-4">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="py-2 pr-4">Username</th>
                <th className="py-2 pr-4">Email</th>
                <th className="py-2 pr-4">Role</th>
                <th className="py-2 pr-4">Active</th>
                <th className="py-2 pr-4">Verified</th>
                <th className="py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {detail.members.map((m) => {
                const isSelf = m.id === user?.id;
                const isPlatformSuperadmin = m.is_superadmin === true;
                // Locking actions for self + platform superadmin
                // matches the backend guard semantics so the UI
                // doesn't dangle affordances that would 400/403.
                const lockedReason = isSelf
                  ? "You cannot modify your own membership here."
                  : isPlatformSuperadmin
                  ? "Platform superadmin, managed elsewhere."
                  : null;
                const busy = memberBusyId === m.id;
                return (
                  <tr key={m.id} className="border-b border-border-subtle align-middle">
                    <td className="py-2 pr-4 text-text-primary">{m.username}</td>
                    <td className="py-2 pr-4 text-text-secondary">{m.email}</td>
                    <td className="py-2 pr-4 text-text-secondary">
                      {lockedReason ? (
                        <span>{m.role}</span>
                      ) : (
                        <label className="sr-only" htmlFor={`role-${m.id}`}>
                          Role for {m.username}
                        </label>
                      )}
                      {!lockedReason && (
                        <select
                          id={`role-${m.id}`}
                          aria-label={`Role for ${m.username}`}
                          value={m.role}
                          disabled={busy}
                          onChange={(e) => {
                            if (e.target.value !== m.role) {
                              patchMember(m, { role: e.target.value });
                            }
                          }}
                          className={`${input} max-w-[10rem]`}
                        >
                          {ROLE_OPTIONS.map((r) => (
                            <option key={r} value={r}>{r}</option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="py-2 pr-4 text-text-secondary">
                      {m.is_active ? "yes" : "no"}
                    </td>
                    <td className="py-2 pr-4 text-text-secondary">
                      {m.email_verified ? "yes" : "no"}
                    </td>
                    <td className="py-2">
                      {lockedReason ? (
                        <span className="text-xs text-text-muted">
                          {lockedReason}
                        </span>
                      ) : (
                        <div className="flex flex-wrap gap-2">
                          {m.is_active ? (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => setDeactivateTarget(m)}
                              className={`${btnDangerSolid} min-h-[44px]`}
                              aria-label={`Deactivate ${m.username}`}
                              title="Revoke access immediately. Data and audit history are preserved; the member can be reactivated later."
                            >
                              Deactivate
                            </button>
                          ) : (
                            <>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() =>
                                  patchMember(m, { is_active: true })
                                }
                                className={`${btnSecondary} min-h-[44px]`}
                                aria-label={`Reactivate ${m.username}`}
                                title="Restore the member's access. They will be required to sign in again."
                              >
                                Reactivate
                              </button>
                              {/*
                                Once a member is deactivated, the system-
                                level hard-delete becomes available. Lives
                                on the dedicated user-detail page so the
                                final "are you sure" + audit emission
                                runs there. Schema reality: User.org_id
                                is a single NOT NULL FK, so "detach from
                                org" without deleting is not expressible
                                today; deactivate + delete is the only
                                terminal path.

                                Gate on ``users.delete`` (not the page-
                                level ``orgs.manage`` gate) — architect
                                feedback on PR #303: the navigation link
                                advertises a destructive system-level
                                action, so it must only render for
                                operators who can actually perform it.
                                A future support role with ``users.view``
                                or ``orgs.manage`` but NOT ``users.delete``
                                should see this row without the link.
                              */}
                              {hasPlatformPermission(user, "users.delete") && (
                                <Link
                                  href={`/admin/users/${m.id}`}
                                  className={`${btnSecondary} min-h-[44px]`}
                                  aria-label={`Open user detail for ${m.username}`}
                                  title="Open the user-detail page to permanently delete this user."
                                >
                                  Delete user…
                                </Link>
                              )}
                            </>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <ConfirmModal
        open={deactivateTarget !== null}
        title="Deactivate member"
        message={
          deactivateTarget
            ? `Deactivate ${deactivateTarget.email}? They will lose access immediately but their data and audit history remain. You can reactivate them later from this page.`
            : ""
        }
        confirmLabel="Deactivate"
        cancelLabel="Cancel"
        variant="danger"
        onConfirm={confirmDeactivateMember}
        onCancel={() => setDeactivateTarget(null)}
      />

      {/* Counts card */}
      <section className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Data</h2>
        </div>
        <div className="grid grid-cols-2 gap-4 px-6 py-5 sm:grid-cols-4">
          <div>
            <p className="text-xs text-text-muted">Transactions</p>
            <p className="text-2xl font-semibold tabular-nums text-text-primary">{detail.counts.transactions}</p>
          </div>
          <div>
            <p className="text-xs text-text-muted">Accounts</p>
            <p className="text-2xl font-semibold tabular-nums text-text-primary">{detail.counts.accounts}</p>
          </div>
          <div>
            <p className="text-xs text-text-muted">Budgets</p>
            <p className="text-2xl font-semibold tabular-nums text-text-primary">{detail.counts.budgets}</p>
          </div>
          <div>
            <p className="text-xs text-text-muted">Forecast plans</p>
            <p className="text-2xl font-semibold tabular-nums text-text-primary">{detail.counts.forecast_plans}</p>
          </div>
        </div>
      </section>

      {/* Danger zone */}
      <section className={`${card} border-danger/40`}>
        <div className={cardHeader}>
          <h2 className={`${cardTitle} text-danger`}>Danger zone</h2>
        </div>
        <div className="px-6 py-5 space-y-3">
          <p className="text-sm text-text-secondary">
            Permanently delete <strong className="text-text-primary">{detail.name}</strong>.
            This removes the org and every transaction, account, budget, plan,
            invitation, and member tied to it. The action cannot be undone.
          </p>
          <p className="text-sm text-text-secondary">
            Type the organization name to confirm:
          </p>
          <input
            type="text"
            aria-label="Confirm organization name"
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
            placeholder={detail.name}
            className={`${input} max-w-sm`}
          />
          <div>
            <button
              type="button"
              onClick={handleDelete}
              disabled={!confirmMatches || deleting}
              className={btnDangerSolid}
            >
              {deleting ? "Deleting…" : "Delete organization"}
            </button>
          </div>
        </div>
      </section>
    </AppShell>
  );
}
