"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import AppShell from "@/components/AppShell";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import Spinner from "@/components/ui/Spinner";
import { pageCount } from "@/lib/hooks/use-table-state";
import type { SortDir } from "@/lib/hooks/use-table-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import type { AuditEvent, AuditEventListResponse } from "@/lib/types";
import {
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
} from "@/lib/styles";

const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_SORT_BY = "created_at";
const DEFAULT_SORT_DIR: SortDir = "desc";

// Backend-whitelisted sort keys. Unknown keys 400, so we clamp a seeded
// URL value back to the default rather than send garbage.
const SORT_FIELDS = [
  "created_at",
  "event_type",
  "actor_email",
  "target_org_name",
  "outcome",
] as const;
type SortField = (typeof SORT_FIELDS)[number];

const PAGE_SIZE_VALUES = [10, 25, 50, 100] as const;

const dtFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function shortRequestId(value: string | null): string {
  if (!value) return "";
  return value.length > 12 ? value.slice(0, 12) : value;
}

export default function AdminAuditPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner />
        </div>
      }
    >
      <AdminAuditPageContent />
    </Suspense>
  );
}

function AdminAuditPageContent() {
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

  const [data, setData] = useState<AuditEventListResponse | null>(null);
  const [error, setError] = useState("");
  const [eventTypeInput, setEventTypeInput] = useState("");
  const [outcomeInput, setOutcomeInput] = useState("");
  const [targetOrgInput, setTargetOrgInput] = useState("");
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
    if (!hasPlatformPermission(user, "audit.view")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "audit.view")) return;
    setFetching(true);
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(offset),
      sort_by: sortBy,
      sort_dir: sortDir,
    });
    if (eventTypeInput.trim()) params.set("event_type", eventTypeInput.trim());
    if (outcomeInput) params.set("outcome", outcomeInput);
    const targetOrg = targetOrgInput.trim();
    if (targetOrg && /^[1-9][0-9]*$/.test(targetOrg)) {
      params.set("target_org_id", targetOrg);
    }
    apiFetch<AuditEventListResponse>(
      `/api/v1/admin/audit?${params.toString()}`,
    )
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [
    loading,
    user,
    eventTypeInput,
    outcomeInput,
    targetOrgInput,
    offset,
    sortBy,
    sortDir,
    pageSize,
  ]);

  // Clamp an over-offset URL back to the last valid page once data lands.
  useEffect(() => {
    if (!data) return;
    if (offset > 0 && offset >= data.total) {
      const lastOffset = Math.max(0, (pageCount(data.total, pageSize) - 1) * pageSize);
      if (lastOffset !== offset) setOffset(lastOffset);
    }
  }, [data, offset, pageSize]);

  // Mirror sort + pagination state back to the URL (router.replace,
  // scroll:false). Filter inputs stay local-only.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "audit.view")) return;
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

  if (loading || !user || !hasPlatformPermission(user, "audit.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Audit log</h1>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Recent events</h2>
        </div>
        <div className="grid grid-cols-1 gap-3 px-6 py-4 sm:grid-cols-3">
          <input
            type="search"
            value={eventTypeInput}
            onChange={(e) => {
              setOffset(0);
              setEventTypeInput(e.target.value);
            }}
            placeholder="Event type (exact)"
            className={input}
            aria-label="Filter by event type"
          />
          <select
            value={outcomeInput}
            onChange={(e) => {
              setOffset(0);
              setOutcomeInput(e.target.value);
            }}
            className={input}
            aria-label="Filter by outcome"
          >
            <option value="">All outcomes</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </select>
          <input
            type="text"
            inputMode="numeric"
            value={targetOrgInput}
            onChange={(e) => {
              setOffset(0);
              setTargetOrgInput(e.target.value);
            }}
            placeholder="Target org ID"
            className={input}
            aria-label="Filter by target org id"
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <SortableHeader
                  label="When"
                  field="created_at"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Event type"
                  field="event_type"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Actor email"
                  field="actor_email"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Target org"
                  field="target_org_name"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Outcome"
                  field="outcome"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <th className="px-6 py-3">Request ID</th>
                <th className="px-6 py-3">IP</th>
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    No audit events match.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row: AuditEvent) => (
                  <tr key={row.id} className="border-b border-border-subtle">
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {dtFmt.format(new Date(row.created_at))}
                    </td>
                    <td className="px-6 py-3 text-text-primary">
                      {row.event_type}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.actor_email}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.target_org_name ?? "-"}
                      {row.target_org_id != null && (
                        <span className="ml-1 text-text-muted">
                          (#{row.target_org_id})
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-3">
                      <span
                        className={
                          row.outcome === "success"
                            ? "rounded-full bg-success/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-success"
                            : "rounded-full bg-danger/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-danger"
                        }
                      >
                        {row.outcome}
                      </span>
                    </td>
                    <td
                      className="px-6 py-3 font-mono text-xs text-text-muted"
                      title={row.request_id ?? ""}
                    >
                      {shortRequestId(row.request_id)}
                    </td>
                    <td className="px-6 py-3 font-mono text-xs text-text-muted">
                      {row.ip_address ?? "-"}
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
