"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Activity,
  Building2,
  CheckCircle2,
  ChevronRight,
  ScrollText,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  badgeBase,
  card,
  cardTitle,
  error as errorCls,
} from "@/lib/styles";
import type { AuditEvent, AuditEventListResponse } from "@/lib/types";

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

type AdminTile = {
  href: string;
  title: string;
  description: string;
  permission: string;
  Icon: typeof Building2;
};

// Catalog of /admin/* sub-pages reachable from the hub. Each tile
// declares the platform permission its destination requires, so users
// only see tiles whose target page they can open. /me does not yet
// return permissions for non-superadmins, so they resolve to false on
// every key — the hub renders empty for them (matches PR #171's gate).
const ADMIN_TILES: readonly AdminTile[] = [
  {
    href: "/admin/orgs",
    title: "Organizations",
    description: "Search, drill into, and manage every org on the platform.",
    permission: "orgs.view",
    Icon: Building2,
  },
  {
    href: "/admin/audit",
    title: "Audit log",
    description:
      "Persisted record of platform actions (subscription overrides, org deletes, tenant resets).",
    permission: "audit.view",
    Icon: ScrollText,
  },
  {
    href: "/admin/roles",
    title: "Roles",
    description: "Manage platform roles and the permissions they grant.",
    permission: "roles.manage",
    Icon: ShieldCheck,
  },
];

// Health pill shape derived from both subsystems. Keeps things color-
// not-alone: an icon + label always travel together, so the pill is
// understandable to screen readers and color-blind users without
// relying on hue.
type HealthSummary = {
  tone: "ok" | "warn" | "down";
  label: string;
  description: string;
  Icon: typeof CheckCircle2;
};

function summarizeHealth(health: DashboardPayload["health"]): HealthSummary {
  const downCells: string[] = [];
  if (!health.db.ok) downCells.push("Database");
  if (!health.redis.ok) downCells.push("Redis");

  if (downCells.length === 0) {
    return {
      tone: "ok",
      label: "All systems ok",
      description: "Database and Redis healthy.",
      Icon: CheckCircle2,
    };
  }
  if (downCells.length === 2) {
    return {
      tone: "down",
      label: "Platform down",
      description: "Database and Redis unreachable.",
      Icon: XCircle,
    };
  }
  return {
    tone: "warn",
    label: "Degraded",
    description: `${downCells[0]} unreachable.`,
    Icon: Activity,
  };
}

function HealthPill({ summary }: { summary: HealthSummary }) {
  // Token-clean tone classes. Each tone uses the existing semantic
  // colour pair already declared in globals.css (success / warning /
  // danger) plus its `-dim` background — no raw palette utilities.
  const toneClass =
    summary.tone === "ok"
      ? "bg-success-dim text-success"
      : summary.tone === "warn"
        ? "bg-warning-dim text-warning"
        : "bg-danger-dim text-danger";
  const Icon = summary.Icon;
  return (
    <span
      className={`${badgeBase} ${toneClass} px-2.5 py-1`}
      role="status"
      aria-label={`Platform health: ${summary.label}. ${summary.description}`}
    >
      <Icon aria-hidden="true" className="h-3.5 w-3.5" />
      <span>{summary.label}</span>
    </span>
  );
}

function HubTile({ tile }: { tile: AdminTile }) {
  const Icon = tile.Icon;
  return (
    <Link
      href={tile.href}
      // Touch targets ≥44px guaranteed by p-5 padding around the title
      // row (h ~ 84px with content); focus-visible ring uses the
      // shared accent token; reduced-motion users get an instant
      // transition because transition-colors is property-scoped (no
      // transforms or opacity fades).
      className={`${card} group relative block p-5 transition-colors hover:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30`}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-accent-dim text-accent"
        >
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-base text-text-primary">
            {tile.title}
          </h2>
          <p className="mt-1 text-sm text-text-secondary">
            {tile.description}
          </p>
        </div>
        <ChevronRight
          aria-hidden="true"
          className="mt-1 h-4 w-4 shrink-0 text-text-muted transition-colors group-hover:text-accent"
        />
      </div>
    </Link>
  );
}

function HealthRow({ name, cell }: { name: string; cell: HealthCell }) {
  const pillClass = cell.ok
    ? "bg-success-dim text-success"
    : "bg-danger-dim text-danger";
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

// Compact relative-time helper. Audit events are timestamps in ISO
// format; the hub only needs a coarse hint ("5 m ago", "2 h ago",
// "3 d ago"). Falls back to the raw date for anything ≥ 30 days.
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diffMs = Date.now() - then;
  if (diffMs < 0) return "just now";
  const m = Math.floor(diffMs / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString();
}

function RecentActivityRow({ event }: { event: AuditEvent }) {
  const ok = event.outcome === "success";
  // Outcome marker pairs an icon with a colour so it satisfies
  // color-not-alone; the icon is decorative and the textual outcome
  // is carried by the aria-label below.
  const toneClass = ok ? "text-success" : "text-danger";
  const Icon = ok ? CheckCircle2 : XCircle;
  return (
    <li className="flex items-center justify-between gap-3 border-b border-border-subtle py-2.5 last:border-0">
      <div className="flex min-w-0 items-center gap-2">
        <Icon
          aria-label={ok ? "success" : "failure"}
          className={`h-4 w-4 shrink-0 ${toneClass}`}
        />
        <div className="min-w-0">
          <p className="truncate text-sm text-text-primary">
            <span className="font-medium">{event.event_type}</span>
            <span className="text-text-muted"> by {event.actor_email}</span>
          </p>
          {event.target_org_name && (
            <p className="truncate text-xs text-text-muted">
              {event.target_org_name}
            </p>
          )}
        </div>
      </div>
      <time
        dateTime={event.created_at}
        className="shrink-0 text-xs tabular-nums text-text-muted"
        title={new Date(event.created_at).toLocaleString()}
      >
        {relativeTime(event.created_at)}
      </time>
    </li>
  );
}

export default function AdminDashboardPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [recent, setRecent] = useState<AuditEvent[] | null>(null);

  // Client-side guard: redirect users without admin.view to /dashboard.
  // The backend gate on admin.view is still authoritative — this just
  // keeps a regular user from seeing a 403 error screen when they
  // somehow land on the URL (old bookmark, manual typing).
  const canViewAdmin = hasPlatformPermission(user, "admin.view");
  const canViewAudit = hasPlatformPermission(user, "audit.view");
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

  // Recent activity is gated on audit.view. We reuse the existing
  // /api/v1/admin/audit endpoint (already powering /admin/audit) with
  // limit=5 to avoid introducing a new backend endpoint. Failures
  // silently leave the panel empty — the dedicated /admin/audit page
  // is the source of truth and surfaces any real fetch errors there.
  useEffect(() => {
    if (authLoading || !canViewAudit) return;
    let cancelled = false;
    (async () => {
      try {
        const payload = await apiFetch<AuditEventListResponse>(
          "/api/v1/admin/audit?limit=5",
        );
        // Defensive: payload.items can be absent if the endpoint
        // returns an unexpected shape (or in tests where a single
        // mocked apiFetch responds to multiple URLs). Treat as empty.
        if (!cancelled) setRecent(Array.isArray(payload?.items) ? payload.items : []);
      } catch {
        if (!cancelled) setRecent([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authLoading, canViewAudit]);

  // Hooks must run in a stable order on every render, so derive the
  // health summary before the guard's early return below.
  const healthSummary = useMemo(
    () => (data ? summarizeHealth(data.health) : null),
    [data],
  );

  // Match the guard pattern used by /system/plans: render nothing until
  // auth has settled AND the user is confirmed to hold admin.view.
  // Prevents a non-permitted user from briefly seeing the admin page
  // shell before the effect above redirects them to /dashboard.
  if (authLoading || !canViewAdmin) return null;

  // Hide tiles whose destination the current user lacks permission to
  // open. Mirrors AppShell's per-link gating so a user never sees a
  // tile they'd be redirected from.
  const visibleTiles = ADMIN_TILES.filter((t) =>
    hasPlatformPermission(user, t.permission),
  );

  return (
    <AppShell>
      <div className="space-y-6">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="font-display text-2xl text-text-primary">Admin</h1>
            <p className="mt-1 text-sm text-text-muted">
              Platform ops hub, totals and shortcuts across all organizations.
            </p>
          </div>
          {healthSummary && <HealthPill summary={healthSummary} />}
        </header>

        {error && <div className={errorCls}>{error}</div>}

        {fetching && !data && (
          <p className="text-sm text-text-muted">Loading…</p>
        )}

        {data && (
          <>
            {/* One-line pulse strip. dl/dt/dd keeps it semantic; the
                visual treatment is a single flex row that wraps on
                narrow viewports. No card chrome — the point is to make
                the numbers feel like a status line, not a dashboard. */}
            <section
              aria-label="Platform totals"
              className={`${card} px-5 py-3`}
            >
              <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
                <div className="flex items-baseline gap-2">
                  <dd className="text-xl font-semibold tabular-nums text-text-primary">
                    {nf.format(data.kpis.total_orgs)}
                  </dd>
                  <dt className="text-xs uppercase tracking-[0.08em] text-text-muted">
                    Organizations
                  </dt>
                </div>
                <div className="flex items-baseline gap-2">
                  <dd className="text-xl font-semibold tabular-nums text-text-primary">
                    {nf.format(data.kpis.total_users)}
                  </dd>
                  <dt className="text-xs uppercase tracking-[0.08em] text-text-muted">
                    Users
                  </dt>
                </div>
                <div className="flex items-baseline gap-2">
                  <dd className="text-xl font-semibold tabular-nums text-text-primary">
                    {nf.format(data.kpis.active_subscriptions)}
                  </dd>
                  <dt className="text-xs uppercase tracking-[0.08em] text-text-muted">
                    Active subscriptions
                  </dt>
                </div>
                <div className="flex items-baseline gap-2">
                  <dd className="text-xl font-semibold tabular-nums text-text-primary">
                    {nf.format(data.kpis.signups_last_7d)}
                  </dd>
                  <dt className="text-xs uppercase tracking-[0.08em] text-text-muted">
                    Signups (7d)
                  </dt>
                </div>
              </dl>
            </section>

            {/* Hub-first nav. The primary affordance on this page is
                navigating to admin sub-areas — make it the largest,
                most prominent block. Two columns on >= sm. */}
            {visibleTiles.length > 0 && (
              <section
                aria-label="Admin sub-areas"
                className="grid grid-cols-1 gap-4 sm:grid-cols-2"
              >
                {visibleTiles.map((t) => (
                  <HubTile key={t.href} tile={t} />
                ))}
              </section>
            )}

            <section className={`${card} p-5`}>
              <h2 className={`${cardTitle} mb-2`}>System health</h2>
              <HealthRow name="Database" cell={data.health.db} />
              <HealthRow name="Redis" cell={data.health.redis} />
            </section>

            {/* Recent activity. Gated on audit.view so the panel only
                renders for users who can already see the audit log.
                We reuse the existing /api/v1/admin/audit endpoint — no
                new backend route needed. */}
            {canViewAudit && recent !== null && recent.length > 0 && (
              <section className={`${card} p-5`}>
                <div className="mb-2 flex items-baseline justify-between gap-2">
                  <h2 className={cardTitle}>Recent activity</h2>
                  <Link
                    href="/admin/audit"
                    className="text-xs text-text-muted hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  >
                    View all
                  </Link>
                </div>
                <ul>
                  {recent.map((evt) => (
                    <RecentActivityRow key={evt.id} event={evt} />
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}
