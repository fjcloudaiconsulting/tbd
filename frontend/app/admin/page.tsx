"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { card, cardTitle, error as errorCls } from "@/lib/styles";

type HealthCell = { ok: boolean; latency_ms?: number; error?: string };

type DashboardPayload = {
  kpis: {
    total_orgs: number;
    total_users: number;
    active_subscriptions: number;
    signups_last_7d: number;
  };
  health: {
    db: HealthCell;
    redis: HealthCell;
  };
};

const nf = new Intl.NumberFormat();

function KpiCard({ label, value }: { label: string; value: number }) {
  return (
    <div className={`${card} p-5`}>
      <p className="text-xs font-medium uppercase tracking-[0.08em] text-text-muted">
        {label}
      </p>
      <p className="mt-2 font-display text-3xl text-text-primary">{nf.format(value)}</p>
    </div>
  );
}

function HealthRow({ name, cell }: { name: string; cell: HealthCell }) {
  const pillClass = cell.ok
    ? "bg-success/10 text-success"
    : "bg-danger/10 text-danger";
  return (
    <div className="flex items-center justify-between gap-4 border-b border-border-subtle py-3 last:border-0">
      <span className="font-medium text-text-primary">{name}</span>
      <div className="flex items-center gap-3 text-xs">
        <span
          className={`rounded-full px-2 py-0.5 font-semibold uppercase tracking-wider ${pillClass}`}
        >
          {cell.ok ? "ok" : "down"}
        </span>
        <span className="text-text-muted">
          {cell.ok ? `${cell.latency_ms} ms` : cell.error ?? "unavailable"}
        </span>
      </div>
    </div>
  );
}

export default function AdminDashboardPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);

  // Client-side guard: redirect non-superadmins to /dashboard. The
  // backend gate on admin.view is still authoritative — this just
  // keeps a regular user from seeing a 403 error screen when they
  // somehow land on the URL (old bookmark, manual typing).
  useEffect(() => {
    if (!authLoading && user && !user.is_superadmin) {
      router.replace("/dashboard");
    }
  }, [user, authLoading, router]);

  useEffect(() => {
    if (authLoading || !user?.is_superadmin) return;
    let cancelled = false;
    (async () => {
      try {
        const payload = await apiFetch<DashboardPayload>("/api/v1/admin/dashboard");
        if (!cancelled) setData(payload);
      } catch (err) {
        if (!cancelled) setError(extractErrorMessage(err, "Failed to load dashboard"));
      } finally {
        if (!cancelled) setFetching(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authLoading, user]);

  // Match the guard pattern used by /system/plans: render nothing until
  // auth has settled AND the user is confirmed superadmin. Prevents a
  // non-superadmin from briefly seeing the admin page shell before the
  // effect above redirects them to /dashboard.
  if (authLoading || !user?.is_superadmin) return null;

  return (
    <AppShell>
      <div className="space-y-6">
        <header>
          <h1 className="font-display text-2xl text-text-primary">Admin</h1>
          <p className="mt-1 text-sm text-text-muted">
            Platform overview — totals across all organizations.
          </p>
        </header>

        {error && <div className={errorCls}>{error}</div>}

        {fetching && !data && (
          <p className="text-sm text-text-muted">Loading…</p>
        )}

        {data && (
          <>
            <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <KpiCard label="Organizations" value={data.kpis.total_orgs} />
              <KpiCard label="Users" value={data.kpis.total_users} />
              <KpiCard label="Active subscriptions" value={data.kpis.active_subscriptions} />
              <KpiCard label="Signups (7d)" value={data.kpis.signups_last_7d} />
            </section>

            <section className={`${card} p-5`}>
              <h2 className={`${cardTitle} mb-2`}>System health</h2>
              <HealthRow name="Database" cell={data.health.db} />
              <HealthRow name="Redis" cell={data.health.redis} />
            </section>
          </>
        )}
      </div>
    </AppShell>
  );
}
