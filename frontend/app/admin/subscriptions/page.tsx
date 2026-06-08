"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import Spinner from "@/components/ui/Spinner";
import { pageCount } from "@/lib/hooks/use-table-state";
import type { SortDir } from "@/lib/hooks/use-table-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  badgeBase,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
} from "@/lib/styles";
import type {
  AdminSubscriptionKPIs,
  AdminSubscriptionListItem,
  AdminSubscriptionListResponse,
  SubscriptionStatus,
} from "@/lib/types";

const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_SORT_BY = "created_at";
const DEFAULT_SORT_DIR: SortDir = "desc";

// Backend-whitelisted sort keys. Unknown keys 400, so we clamp a seeded
// URL value back to the default rather than send garbage.
const SORT_FIELDS = [
  "org_name",
  "plan_slug",
  "status",
  "trial_end",
  "current_period_end",
  "created_at",
] as const;
type SortField = (typeof SORT_FIELDS)[number];

const PAGE_SIZE_VALUES = [10, 25, 50, 100] as const;

// Status filter chips. ``null`` represents "All", which clears the
// filter — kept first in the list so an admin paging through the
// table can collapse a filter without hunting.
const STATUS_FILTERS: {
  value: SubscriptionStatus | null;
  label: string;
}[] = [
  { value: null, label: "All" },
  { value: "active", label: "Active" },
  { value: "trialing", label: "Trial" },
  { value: "past_due", label: "Past due" },
  { value: "canceled", label: "Cancelled" },
];

function StatusBadge({ status }: { status: SubscriptionStatus }) {
  // Status → tone mapping. Active = success, trial = info, past_due
  // = warning, cancelled = neutral. Each pairs an icon-free badge
  // with the textual status so colour is not the only carrier.
  const toneClass =
    status === "active"
      ? "bg-success-dim text-success"
      : status === "trialing"
        ? "bg-info-dim text-info"
        : status === "past_due"
          ? "bg-warning-dim text-warning"
          : "bg-surface-raised text-text-secondary";
  return (
    <span className={`${badgeBase} ${toneClass}`}>
      {status === "past_due" ? "past due" : status}
    </span>
  );
}

function MockBadge() {
  // The single source of "this isn't real money" label. Used on the
  // KPI strip's revenue tiles and on the list table's plan column.
  // Lives here (not in styles.ts) because it carries copy too.
  return (
    <span
      className="ml-1 rounded-sm bg-warning-dim px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider text-warning"
      title="Payments are not live yet. Revenue figures are mocked until L2 ships."
    >
      mock
    </span>
  );
}

type KpiTile = {
  label: string;
  value: string | number;
  hint?: string;
  isMock?: boolean;
};

function KpiStrip({ kpis }: { kpis: AdminSubscriptionKPIs }) {
  const tiles: KpiTile[] = [
    { label: "Total", value: kpis.total_subscriptions },
    { label: "Active", value: kpis.active },
    { label: "Trial", value: kpis.trial },
    { label: "Past due", value: kpis.past_due },
    { label: "Cancelled", value: kpis.cancelled },
    {
      label: "Signups (7d)",
      value: kpis.signups_last_7d,
      hint: "New subscriptions in the last 7 days",
    },
    {
      label: "Trial expiring (7d)",
      value: kpis.trial_expiring_next_7d,
      hint: "Trials whose trial_end falls in the next 7 days",
    },
    {
      label: "MRR",
      value: `$${kpis.mock_mrr}`,
      isMock: true,
      hint: "Mock, real payments not integrated yet (L2.2 parked)",
    },
    {
      label: "ARR",
      value: `$${kpis.mock_arr}`,
      isMock: true,
      hint: "Mock, real payments not integrated yet (L2.2 parked)",
    },
  ];
  return (
    <section
      aria-label="Subscription totals"
      className={`${card} px-5 py-3`}
    >
      <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        {tiles.map((t) => (
          <div
            key={t.label}
            className="flex items-baseline gap-2"
            title={t.hint}
          >
            <dd className="text-xl font-semibold tabular-nums text-text-primary">
              {t.value}
            </dd>
            <dt className="text-xs uppercase tracking-[0.08em] text-text-muted">
              {t.label}
              {t.isMock && <MockBadge />}
            </dt>
          </div>
        ))}
      </dl>
    </section>
  );
}

function PlanDistribution({ kpis }: { kpis: AdminSubscriptionKPIs }) {
  if (kpis.plan_distribution.length === 0) return null;
  return (
    <section className={`${card}`}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Plan distribution</h2>
      </div>
      <ul className="px-6 py-3">
        {kpis.plan_distribution.map((p) => (
          <li
            key={p.plan_id ?? p.plan_slug ?? p.plan_name ?? "unknown"}
            className="flex items-center justify-between border-b border-border-subtle py-2 last:border-0"
          >
            <span className="text-sm text-text-primary">
              {p.plan_name ?? p.plan_slug ?? "Unknown plan"}
              {p.plan_slug && (
                <span className="ml-2 text-xs text-text-muted">
                  {p.plan_slug}
                </span>
              )}
            </span>
            <span className="tabular-nums text-sm text-text-secondary">
              {p.subscription_count}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function AdminSubscriptionsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner />
        </div>
      }
    >
      <AdminSubscriptionsPageContent />
    </Suspense>
  );
}

function AdminSubscriptionsPageContent() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const initialPageSize = (() => {
    const raw = searchParams.get("page_size");
    if (raw === null || raw === "") return DEFAULT_PAGE_SIZE;
    const n = Number(raw);
    return (PAGE_SIZE_VALUES as readonly number[]).includes(n)
      ? n
      : DEFAULT_PAGE_SIZE;
  })();
  const initialOffset = (() => {
    const raw = searchParams.get("offset");
    if (raw === null || raw === "") return 0;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) return 0;
    return Math.floor(n / initialPageSize) * initialPageSize;
  })();
  const initialSortBy: SortField = (() => {
    const raw = searchParams.get("sort_by");
    return raw && (SORT_FIELDS as readonly string[]).includes(raw)
      ? (raw as SortField)
      : DEFAULT_SORT_BY;
  })();
  const initialSortDir: SortDir = (() => {
    const raw = searchParams.get("sort_dir");
    return raw === "asc" || raw === "desc" ? raw : DEFAULT_SORT_DIR;
  })();

  const [data, setData] = useState<AdminSubscriptionListResponse | null>(null);
  const [kpis, setKpis] = useState<AdminSubscriptionKPIs | null>(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] =
    useState<SubscriptionStatus | null>(null);
  const [planFilter, setPlanFilter] = useState<string | null>(null);
  const [offset, setOffset] = useState(initialOffset);
  const [sortBy, setSortBy] = useState<SortField>(initialSortBy);
  const [sortDir, setSortDir] = useState<SortDir>(initialSortDir);
  const [pageSize, setPageSize] = useState(initialPageSize);
  const [fetching, setFetching] = useState(true);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "subscriptions.view")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  // KPIs load once per page mount. Refetching on every filter change
  // would be misleading — the KPI strip is platform-wide, not
  // filtered-list-wide.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "subscriptions.view")) {
      return;
    }
    apiFetch<AdminSubscriptionKPIs>("/api/v1/admin/subscriptions/kpis")
      .then((d) => setKpis(d))
      .catch(() => {
        // KPI failures are non-fatal — the table is the primary
        // affordance. Errors surface via the list-fetch path below.
      });
  }, [loading, user]);

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "subscriptions.view")) {
      return;
    }
    setFetching(true);
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(offset),
      sort_by: sortBy,
      sort_dir: sortDir,
    });
    if (q.trim()) params.set("q", q.trim());
    if (statusFilter) params.set("status", statusFilter);
    if (planFilter) params.set("plan", planFilter);
    apiFetch<AdminSubscriptionListResponse>(
      `/api/v1/admin/subscriptions?${params.toString()}`,
    )
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, q, statusFilter, planFilter, offset, sortBy, sortDir, pageSize]);

  // Clamp an over-offset URL back to the last valid page once data lands.
  useEffect(() => {
    if (!data) return;
    if (offset > 0 && offset >= data.total) {
      const lastOffset = Math.max(0, (pageCount(data.total, pageSize) - 1) * pageSize);
      if (lastOffset !== offset) setOffset(lastOffset);
    }
  }, [data, offset, pageSize]);

  // Mirror sort + pagination state back to the URL (router.replace,
  // scroll:false). q / status / plan stay local-only — they are
  // filter chips whose state does not need to survive a refresh here.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "subscriptions.view")) {
      return;
    }
    const params = new URLSearchParams();
    if (offset > 0) params.set("offset", String(offset));
    if (sortBy !== DEFAULT_SORT_BY) params.set("sort_by", sortBy);
    if (sortDir !== DEFAULT_SORT_DIR) params.set("sort_dir", sortDir);
    if (pageSize !== DEFAULT_PAGE_SIZE) params.set("page_size", String(pageSize));
    const query = params.toString();
    const current = searchParams.toString();
    if (query === current) return;
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, user, offset, sortBy, sortDir, pageSize, pathname, router]);

  const handleSort = useCallback(
    (field: string) => {
      if (!(SORT_FIELDS as readonly string[]).includes(field)) return;
      const f = field as SortField;
      setSortBy(f);
      setSortDir(f === sortBy ? (sortDir === "asc" ? "desc" : "asc") : "asc");
      setOffset(0);
    },
    [sortBy, sortDir],
  );

  const planChips = useMemo(() => {
    if (!kpis) return [] as { slug: string; name: string; count: number }[];
    return kpis.plan_distribution
      .filter((p) => p.plan_slug !== null)
      .map((p) => ({
        slug: p.plan_slug as string,
        name: p.plan_name ?? (p.plan_slug as string),
        count: p.subscription_count,
      }));
  }, [kpis]);

  if (loading || !user || !hasPlatformPermission(user, "subscriptions.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex items-start gap-1">
          <h1 className={`${pageTitle} mb-0`}>Subscriptions</h1>
          <HelpAnchor
            section="admin"
            label="Subscriptions"
            variant="inline-title"
          />
        </div>
      </div>

      <p className="mb-4 text-sm text-text-muted">
        Cross-org subscription view. Revenue figures are mock until payments
        ($0, payments not live).
      </p>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      {kpis && (
        <div className="mb-6 space-y-6">
          <KpiStrip kpis={kpis} />
          <PlanDistribution kpis={kpis} />
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>All subscriptions</h2>
        </div>
        <div className="space-y-3 px-6 py-4">
          <div className="flex flex-wrap items-center gap-2">
            {STATUS_FILTERS.map((s) => {
              const active = statusFilter === s.value;
              return (
                <button
                  key={s.label}
                  type="button"
                  onClick={() => {
                    setOffset(0);
                    setStatusFilter(s.value);
                  }}
                  aria-pressed={active}
                  className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${
                    active
                      ? "border-accent bg-accent-dim text-accent"
                      : "border-border text-text-secondary hover:border-accent hover:text-accent"
                  }`}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
          {planChips.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs uppercase tracking-wider text-text-muted">
                Plan
              </span>
              <button
                type="button"
                onClick={() => {
                  setOffset(0);
                  setPlanFilter(null);
                }}
                aria-pressed={planFilter === null}
                className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${
                  planFilter === null
                    ? "border-accent bg-accent-dim text-accent"
                    : "border-border text-text-secondary hover:border-accent hover:text-accent"
                }`}
              >
                All
              </button>
              {planChips.map((p) => (
                <button
                  key={p.slug}
                  type="button"
                  onClick={() => {
                    setOffset(0);
                    setPlanFilter(p.slug);
                  }}
                  aria-pressed={planFilter === p.slug}
                  className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${
                    planFilter === p.slug
                      ? "border-accent bg-accent-dim text-accent"
                      : "border-border text-text-secondary hover:border-accent hover:text-accent"
                  }`}
                >
                  {p.name}
                  <span className="ml-1 text-text-muted">{p.count}</span>
                </button>
              ))}
            </div>
          )}
          <input
            type="search"
            value={q}
            onChange={(e) => {
              setOffset(0);
              setQ(e.target.value);
            }}
            placeholder="Search by org name or plan slug…"
            className={`${input} w-full max-w-sm`}
            aria-label="Search subscriptions"
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <SortableHeader
                  label="Organization"
                  field="org_name"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Plan"
                  field="plan_slug"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Status"
                  field="status"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Trial ends"
                  field="trial_end"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Period ends"
                  field="current_period_end"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Created"
                  field="created_at"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-6 py-10 text-center text-text-muted"
                  >
                    <p className="font-medium text-text-secondary">
                      No subscriptions match the current filters.
                    </p>
                    <p className="mt-1 text-xs">
                      Clear filters or widen the search.
                    </p>
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row: AdminSubscriptionListItem) => (
                  <tr
                    key={row.subscription_id}
                    className="border-b border-border-subtle"
                  >
                    <td className="px-6 py-3">
                      <Link
                        href={`/admin/subscriptions/${row.subscription_id}`}
                        className="text-accent hover:text-accent-hover"
                      >
                        {row.org_name}
                      </Link>
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.plan_name ?? row.plan_slug ?? "—"}
                    </td>
                    <td className="px-6 py-3">
                      <StatusBadge status={row.status} />
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.trial_end ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.current_period_end ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.created_at?.slice(0, 10) ?? "—"}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>

        {data && (data.total > pageSize || offset > 0) && (
          <div className="px-6">
            <Pagination
              page={Math.max(1, Math.floor(offset / pageSize) + 1)}
              pageSize={pageSize}
              total={data.total}
              onPageChange={(n) => setOffset((n - 1) * pageSize)}
              onPageSizeChange={(n) => {
                setPageSize(n);
                setOffset(0);
              }}
            />
          </div>
        )}
      </div>
    </AppShell>
  );
}
