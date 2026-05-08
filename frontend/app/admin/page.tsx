"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
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

type AdminCard = {
  href: string;
  title: string;
  description: string;
  permission: string;
};

// Catalog of /admin/* sub-pages reachable from the hub. Each card
// declares the platform permission its destination requires, so users
// only see cards whose target page they can open. Today /me does not
// return permissions, so non-superadmins resolve to false on every
// key — the hub renders empty for them, matching PR #171's gate.
const ADMIN_CARDS: readonly AdminCard[] = [
  {
    href: "/admin/orgs",
    title: "Organizations",
    description: "Search, drill into, and manage every org on the platform.",
    permission: "orgs.view",
  },
  {
    href: "/admin/audit",
    title: "Audit log",
    description:
      "Persisted record of platform actions (subscription overrides, org deletes, tenant resets).",
    permission: "audit.view",
  },
  {
    href: "/admin/roles",
    title: "Roles",
    description: "Manage platform roles and the permissions they grant.",
    permission: "roles.manage",
  },
];

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

  // Client-side guard: redirect users without admin.view to /dashboard.
  // The backend gate on admin.view is still authoritative — this just
  // keeps a regular user from seeing a 403 error screen when they
  // somehow land on the URL (old bookmark, manual typing).
  const canViewAdmin = hasPlatformPermission(user, "admin.view");
  // Two-branch guard: AppShell can't redirect from a null render, so we
  // explicitly send unauthenticated visitors to /login and authenticated
  // users without admin.view to /dashboard. Pre-existing bug: previous
  // single-branch effect skipped the !user case entirely.
  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canViewAdmin) {
      router.replace("/dashboard");
      return;
    }
  }, [user, authLoading, canViewAdmin, router]);

  useEffect(() => {
    if (authLoading || !canViewAdmin) return;
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
  }, [authLoading, canViewAdmin]);

  // Match the guard pattern used by /system/plans: render nothing until
  // auth has settled AND the user is confirmed to hold admin.view.
  // Prevents a non-permitted user from briefly seeing the admin page
  // shell before the effect above redirects them to /dashboard.
  if (authLoading || !canViewAdmin) return null;

  // Hide cards whose destination the current user lacks permission to
  // open. Mirrors AppShell's per-link gating so a user never sees a
  // card they'd be redirected from.
  const visibleAdminCards = ADMIN_CARDS.filter((c) =>
    hasPlatformPermission(user, c.permission),
  );

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

            {visibleAdminCards.length > 0 && (
              <section className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                {visibleAdminCards.map((c) => (
                  <Link
                    key={c.href}
                    href={c.href}
                    className={`${card} block p-5 transition-colors hover:border-accent`}
                  >
                    <h2 className={`${cardTitle} mb-1`}>{c.title}</h2>
                    <p className="text-sm text-text-secondary">{c.description}</p>
                  </Link>
                ))}
              </section>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}
