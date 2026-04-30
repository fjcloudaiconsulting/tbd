"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isSuperadmin } from "@/lib/auth";
import {
  btnPrimary,
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
};

type OrgDetail = {
  id: number;
  name: string;
  billing_cycle_day: number;
  created_at: string | null;
  subscription: Subscription | Record<string, never>;
  members: Member[];
  counts: { transactions: number; accounts: number; budgets: number; forecast_plans: number };
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

  // Delete confirmation
  const [confirmName, setConfirmName] = useState("");
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!isSuperadmin(user)) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  async function refresh() {
    try {
      const d = await apiFetch<OrgDetail>(`/api/v1/admin/orgs/${orgId}`);
      setDetail(d);
      const sub = d.subscription as Subscription;
      setSubStatus(sub.status ?? "");
      setSubTrialEnd(sub.trial_end ?? "");
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load"));
    }
  }

  useEffect(() => {
    if (loading || !user || !isSuperadmin(user) || !orgId) return;
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

  if (loading || !user || !isSuperadmin(user)) {
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

          <form onSubmit={saveSubscription} className="flex flex-col gap-3 sm:flex-row sm:items-end">
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
            <button type="submit" className={btnPrimary}>Save</button>
          </form>
        </div>
      </section>

      {/* Members card */}
      <section className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Members ({detail.members.length})</h2>
        </div>
        <div className="overflow-x-auto px-6 py-4">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="py-2 pr-4">Username</th>
                <th className="py-2 pr-4">Email</th>
                <th className="py-2 pr-4">Role</th>
                <th className="py-2 pr-4">Active</th>
                <th className="py-2">Verified</th>
              </tr>
            </thead>
            <tbody>
              {detail.members.map((m) => (
                <tr key={m.id} className="border-b border-border-subtle">
                  <td className="py-2 pr-4 text-text-primary">{m.username}</td>
                  <td className="py-2 pr-4 text-text-secondary">{m.email}</td>
                  <td className="py-2 pr-4 text-text-secondary">{m.role}</td>
                  <td className="py-2 pr-4 text-text-secondary">{m.is_active ? "yes" : "no"}</td>
                  <td className="py-2 text-text-secondary">{m.email_verified ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Counts card */}
      <section className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Data</h2>
        </div>
        <div className="grid grid-cols-2 gap-4 px-6 py-5 sm:grid-cols-4">
          <div>
            <p className="text-xs text-text-muted">Transactions</p>
            <p className="font-display text-2xl text-text-primary">{detail.counts.transactions}</p>
          </div>
          <div>
            <p className="text-xs text-text-muted">Accounts</p>
            <p className="font-display text-2xl text-text-primary">{detail.counts.accounts}</p>
          </div>
          <div>
            <p className="text-xs text-text-muted">Budgets</p>
            <p className="font-display text-2xl text-text-primary">{detail.counts.budgets}</p>
          </div>
          <div>
            <p className="text-xs text-text-muted">Forecast plans</p>
            <p className="font-display text-2xl text-text-primary">{detail.counts.forecast_plans}</p>
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
              className="rounded-md bg-danger px-4 py-2 text-sm font-medium text-white hover:bg-danger/90 disabled:opacity-50"
            >
              {deleting ? "Deleting…" : "Delete organization"}
            </button>
          </div>
        </div>
      </section>
    </AppShell>
  );
}
