"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import AppShell from "@/components/AppShell";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import Spinner from "@/components/ui/Spinner";
import { pageCount } from "@/lib/hooks/use-table-state";
import type { SortDir } from "@/lib/hooks/use-table-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
  success as successCls,
} from "@/lib/styles";

// Admin orgs list. URL-synced sort + pagination mirrors
// /admin/users/page.tsx (the reference implementation). The query
// string is the source of truth for q / offset / sort / page_size; we
// seed React state from it on first render and mirror state back via
// router.replace so a refreshed or shared URL keeps the table state.

type OrgRow = {
  id: number;
  name: string;
  plan_slug: string | null;
  subscription_status: string | null;
  trial_end: string | null;
  user_count: number;
  active_user_count: number;
  created_at: string | null;
  last_user_created_at: string | null;
};

type OrgListResponse = {
  items: OrgRow[];
  total: number;
  limit: number;
  offset: number;
};

const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_SORT_BY = "created_at";
const DEFAULT_SORT_DIR: SortDir = "desc";
const SEARCH_DEBOUNCE_MS = 300;

// Backend-whitelisted sort keys. Unknown keys 400, so we clamp a seeded
// URL value back to the default rather than send garbage.
const SORT_FIELDS = [
  "name",
  "created_at",
  "plan_slug",
  "subscription_status",
  "user_count",
  "active_user_count",
  "last_user_created_at",
] as const;
type SortField = (typeof SORT_FIELDS)[number];

const PAGE_SIZE_VALUES = [10, 25, 50, 100] as const;

export default function AdminOrgsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner />
        </div>
      }
    >
      <AdminOrgsPageContent />
    </Suspense>
  );
}

function AdminOrgsPageContent() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const initialQ = searchParams.get("q") ?? "";
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

  const [qInput, setQInput] = useState(initialQ);
  const [q, setQ] = useState(initialQ);
  const [offset, setOffset] = useState(initialOffset);
  const [sortBy, setSortBy] = useState<SortField>(initialSortBy);
  const [sortDir, setSortDir] = useState<SortDir>(initialSortDir);
  const [pageSize, setPageSize] = useState(initialPageSize);

  const [data, setData] = useState<OrgListResponse | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [sweepConfirmOpen, setSweepConfirmOpen] = useState(false);
  const [sweepBusy, setSweepBusy] = useState(false);
  const [sweepNotice, setSweepNotice] = useState("");

  async function runSweep() {
    setSweepConfirmOpen(false);
    setSweepBusy(true);
    setSweepNotice("");
    setError("");
    try {
      const res = await apiFetch<{ deleted_count: number }>(
        "/api/v1/admin/orgs/feature-overrides/sweep-expired",
        { method: "POST" },
      );
      setSweepNotice(`Removed ${res.deleted_count} expired overrides.`);
    } catch (err) {
      setError(extractErrorMessage(err, "Sweep failed"));
    } finally {
      setSweepBusy(false);
    }
  }

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "orgs.view")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  // Debounce search input; resets offset to 0 only when the trimmed query
  // actually changes. On mount, qInput and q are both seeded from the same
  // URL params, so next === prev and the seeded offset is left intact (no
  // explicit first-run guard needed).
  useEffect(() => {
    const handle = setTimeout(() => {
      setQ((prev) => {
        const next = qInput.trim();
        if (next !== prev) setOffset(0);
        return next;
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [qInput]);

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "orgs.view")) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loading flag set before an in-effect fetch; proper fix arrives with the SWR data-hook migration
    setFetching(true);
    setError("");
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(offset),
      sort_by: sortBy,
      sort_dir: sortDir,
    });
    if (q) params.set("q", q);
    apiFetch<OrgListResponse>(`/api/v1/admin/orgs?${params.toString()}`)
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, q, offset, sortBy, sortDir, pageSize]);

  // Clamp an over-offset URL back to the last valid page once data lands.
  useEffect(() => {
    if (!data) return;
    if (offset > 0 && offset >= data.total) {
      const lastOffset = Math.max(0, (pageCount(data.total, pageSize) - 1) * pageSize);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clamp URL-owned offset after data lands; not derivable during render
      if (lastOffset !== offset) setOffset(lastOffset);
    }
  }, [data, offset, pageSize]);

  // Mirror state back to the URL (router.replace, scroll:false).
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "orgs.view")) return;
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (offset > 0) params.set("offset", String(offset));
    if (sortBy !== DEFAULT_SORT_BY) params.set("sort_by", sortBy);
    if (sortDir !== DEFAULT_SORT_DIR) params.set("sort_dir", sortDir);
    if (pageSize !== DEFAULT_PAGE_SIZE) params.set("page_size", String(pageSize));
    const query = params.toString();
    const current = searchParams.toString();
    if (query === current) return;
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, user, q, offset, sortBy, sortDir, pageSize, pathname, router]);

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

  if (loading || !user || !hasPlatformPermission(user, "orgs.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between gap-4">
        <h1 className={`${pageTitle} mb-0`}>Organizations</h1>
        <button
          type="button"
          onClick={() => setSweepConfirmOpen(true)}
          disabled={sweepBusy}
          className={btnSecondary}
        >
          {sweepBusy ? "Sweeping…" : "Sweep expired overrides"}
        </button>
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      {sweepNotice && (
        <div className={`${successCls} mb-4`} role="status">
          {sweepNotice}
        </div>
      )}

      <ConfirmModal
        open={sweepConfirmOpen}
        title="Sweep expired overrides"
        message="Permanently delete every feature override row whose expires_at is in the past. This cannot be undone."
        confirmLabel="Sweep"
        cancelLabel="Cancel"
        variant="warning"
        onConfirm={runSweep}
        onCancel={() => setSweepConfirmOpen(false)}
      />

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>All organizations</h2>
        </div>
        <div className="px-6 py-4">
          <input
            type="search"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="Search by name…"
            className={`${input} w-full max-w-sm`}
            aria-label="Search organizations"
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <SortableHeader
                  label="Name"
                  field="name"
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
                  field="subscription_status"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Users"
                  field="user_count"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Newest member"
                  field="last_user_created_at"
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
                  <td colSpan={6} className="px-6 py-6 text-center text-text-muted">
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-6 py-6 text-center text-text-muted">
                    No organizations match.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row) => (
                  <tr key={row.id} className="border-b border-border-subtle">
                    <td className="px-6 py-3">
                      <Link
                        href={`/admin/orgs/${row.id}`}
                        className="text-accent hover:text-accent-hover"
                      >
                        {row.name}
                      </Link>
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.plan_slug ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.subscription_status ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.active_user_count} / {row.user_count}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.last_user_created_at?.slice(0, 10) ?? "—"}
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
