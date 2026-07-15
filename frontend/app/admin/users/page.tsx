"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
} from "@/lib/styles";

// L4.4 cross-org user search list. Mirrors /admin/orgs/page.tsx in
// shape: header + search input + filter chips + paginated table.
//
// URL state contract:
//   The query string is the source of truth for filter state. On
//   mount we read q / org_id / role / status / offset from
//   ``useSearchParams`` and seed React state from them. Filter
//   changes are mirrored back to the URL via ``router.replace`` so:
//     - refreshing keeps the filters
//     - a filtered URL is shareable / linkable
//   Filter changes do NOT create separate history entries. The
//   choice is deliberate ``router.replace`` rather than
//   ``router.push``: every keystroke or chip tap would otherwise
//   become a back-button stop, and admin filter UX rarely benefits
//   from that. Back-button restoration of filter state is therefore
//   out of contract; it requires a future ``router.push``-on-commit
//   path (debounce settled, URL actually changed).
//
//   The URL write is debounced through the same 300 ms ``q``
//   debounce so a keypress sequence does not stomp ``router.replace``
//   on every character. Other (single-tap) filters update the URL
//   eagerly.
//
// Mounted under a top-level <Suspense> because ``useSearchParams`` is
// a client boundary in Next 15.

type OrgRef = {
  org_id: number;
  name: string;
  role: string;
};

type UserRow = {
  id: number;
  email: string;
  username: string;
  display_name: string | null;
  is_superadmin: boolean;
  is_active: boolean;
  email_verified: boolean;
  mfa_enabled: boolean;
  password_changed_at: string | null;
  onboarded_at: string | null;
  created_at: string | null;
  orgs: OrgRef[];
};

type UsersListResponse = {
  items: UserRow[];
  total: number;
  limit: number;
  offset: number;
};

type OrgPickerOption = {
  id: number;
  name: string;
};

type OrgsListResponse = {
  items: { id: number; name: string }[];
  total: number;
};

const DEFAULT_PAGE_SIZE = 25;
const DEFAULT_SORT_BY = "created_at";
const DEFAULT_SORT_DIR: SortDir = "desc";
const SEARCH_DEBOUNCE_MS = 300;
const ROLE_OPTIONS = ["owner", "admin", "member"] as const;
const STATUS_OPTIONS = ["active", "inactive", "unverified", "superadmin"] as const;

// Backend-whitelisted sort keys. Unknown keys 400, so we clamp the
// seeded URL value back to the default rather than send garbage.
const SORT_FIELDS = ["created_at", "email", "username", "role", "org_name"] as const;
type SortField = (typeof SORT_FIELDS)[number];

const PAGE_SIZE_VALUES = [10, 25, 50, 100] as const;

function chipClass(active: boolean): string {
  return [
    "inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium transition-colors",
    active
      ? "border-accent bg-accent/10 text-accent"
      : "border-border bg-surface text-text-secondary hover:bg-surface-raised hover:border-border hover:text-text-primary",
  ].join(" ");
}

export default function AdminUsersPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner />
        </div>
      }
    >
      <AdminUsersPageContent />
    </Suspense>
  );
}

function AdminUsersPageContent() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Seed filter state from the URL on the FIRST render so a refresh
  // (or a shared / bookmarked filtered URL) lands the page in the
  // same state. We rely on these reads being stable across the first
  // render only; subsequent param changes flow through React state.
  const initialQ = searchParams.get("q") ?? "";
  const initialOrgId = (() => {
    const raw = searchParams.get("org_id");
    if (raw === null || raw === "") return "" as const;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : ("" as const);
  })();
  const initialRole = searchParams.get("role") ?? "";
  const initialStatus = searchParams.get("status") ?? "";
  // Parse page_size FIRST so the offset normalization can snap to a
  // multiple of the resolved page boundary. This ensures a shared URL
  // like ?page_size=25&offset=5 lands on a consistent page (offset→0)
  // rather than sending an off-boundary offset to the backend.
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
    // Snap to the nearest lower page boundary for the resolved pageSize.
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

  // Filter state.
  const [qInput, setQInput] = useState(initialQ);
  const [q, setQ] = useState(initialQ);
  const [orgId, setOrgId] = useState<number | "">(initialOrgId);
  const [role, setRole] = useState<string>(initialRole);
  const [status, setStatus] = useState<string>(initialStatus);
  const [offset, setOffset] = useState(initialOffset);
  const [sortBy, setSortBy] = useState<SortField>(initialSortBy);
  const [sortDir, setSortDir] = useState<SortDir>(initialSortDir);
  const [pageSize, setPageSize] = useState(initialPageSize);

  const [data, setData] = useState<UsersListResponse | null>(null);
  const [orgOptions, setOrgOptions] = useState<OrgPickerOption[]>([]);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");

  // Permission gate.
  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "users.view")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  // Load the org picker once. Capped at 200 rows because that is the
  // backend's cap; the picker is a dropdown not an infinite list.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "users.view")) return;
    apiFetch<OrgsListResponse>("/api/v1/admin/orgs?limit=200")
      .then((d) => setOrgOptions(d.items.map((o) => ({ id: o.id, name: o.name }))))
      .catch(() => {
        // Silent: a missing picker degrades the filter UX but doesn't
        // block the list itself. The list page still works.
      });
  }, [loading, user]);

  // Debounce the search input. Resets offset to 0 whenever the user
  // types a new query.
  //
  // First-mount guard: the effect fires once on mount because qInput
  // was seeded from the URL. Without the guard, that first run would
  // call ``setOffset(0)`` after the debounce window and clobber an
  // ``offset=50`` (or any non-zero) value we just seeded from the
  // URL. The ref flips on the first run so subsequent (user-driven)
  // qInput changes still reset the offset, which is the contract for
  // a new search.
  const isInitialDebounceRunRef = useRef(true);
  useEffect(() => {
    if (isInitialDebounceRunRef.current) {
      isInitialDebounceRunRef.current = false;
      return;
    }
    const handle = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [qInput]);

  // Fetch the list. Re-runs whenever any filter changes.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "users.view")) return;
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
    if (orgId !== "") params.set("org_id", String(orgId));
    if (role) params.set("role", role);
    if (status) params.set("status", status);
    apiFetch<UsersListResponse>(`/api/v1/admin/users?${params.toString()}`)
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, q, orgId, role, status, offset, sortBy, sortDir, pageSize]);

  // Clamp an over-offset URL back to the last valid page once the data
  // lands. A shared URL like ?offset=9999&page_size=25 would render
  // "Page 400 of 1" and require ~399 Previous clicks to reach data.
  // This effect fires once per data load: if offset is past the end,
  // snap it down to the last page boundary. After snapping,
  // offset < data.total (or 0 when total is 0), so the guard won't
  // re-fire and the effect doesn't loop.
  useEffect(() => {
    if (!data) return;
    if (offset > 0 && offset >= data.total) {
      const lastOffset = Math.max(0, (pageCount(data.total, pageSize) - 1) * pageSize);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clamp URL-owned offset after data lands; not derivable during render
      if (lastOffset !== offset) setOffset(lastOffset);
    }
  }, [data, offset, pageSize]);

  // Mirror filter state back to the URL. Uses ``router.replace`` so
  // filter changes do not pile up as back-button stops (see the
  // top-of-file URL state contract for the trade-off). ``q`` is
  // already debounced upstream (the qInput effect commits to ``q``
  // after 300 ms); other filters tap and apply, so they write to
  // the URL eagerly.
  //
  // ``scroll: false`` keeps the table position stable across writes;
  // without it Next 15 scrolls to the top of the page on every
  // ``router.replace``.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "users.view")) return;
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (orgId !== "") params.set("org_id", String(orgId));
    if (role) params.set("role", role);
    if (status) params.set("status", status);
    if (offset > 0) params.set("offset", String(offset));
    if (sortBy !== DEFAULT_SORT_BY) params.set("sort_by", sortBy);
    if (sortDir !== DEFAULT_SORT_DIR) params.set("sort_dir", sortDir);
    if (pageSize !== DEFAULT_PAGE_SIZE) params.set("page_size", String(pageSize));
    const query = params.toString();
    // Skip the write when the URL already matches. Cheap string
    // compare; avoids a needless ``router.replace`` (and the React
    // re-render it triggers) on first mount when state was seeded
    // from the URL.
    const current = searchParams.toString();
    if (query === current) return;
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    loading,
    user,
    q,
    orgId,
    role,
    status,
    offset,
    sortBy,
    sortDir,
    pageSize,
    pathname,
    router,
  ]);

  // Header click: switch to the clicked column ascending, or toggle the
  // direction if it is already the active column. Either way reset to
  // the first page (offset 0) so the user isn't stranded on a deep page.
  //
  // Guard: ``SortableHeader`` passes its ``field`` prop as a plain
  // string. Unknown values would reach the backend as an invalid
  // ``sort_by`` and cause a 400. We no-op early for anything not in the
  // whitelisted set so a mis-wired column silently does nothing rather
  // than erroring the table.
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

  const filtersActive = useMemo(
    () => Boolean(q || orgId !== "" || role || status),
    [q, orgId, role, status],
  );

  function resetFilters() {
    setQInput("");
    setQ("");
    setOrgId("");
    setRole("");
    setStatus("");
    setOffset(0);
  }

  if (loading || !user || !hasPlatformPermission(user, "users.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-start justify-between gap-4">
        <div className="flex items-start gap-2">
          <h1 className={`${pageTitle} mb-0`}>Users</h1>
          <HelpAnchor section="admin-users" label="Users admin" variant="inline-title" />
        </div>
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>All users</h2>
        </div>

        {/* Search row */}
        <div className="px-6 py-4">
          <input
            type="search"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="Search by email, username, or name"
            className={`${input} w-full max-w-sm`}
            aria-label="Search users"
          />
        </div>

        {/* Filter chips */}
        <div className="flex flex-wrap items-center gap-2 px-6 pb-4">
          <span className="text-xs uppercase tracking-wider text-text-muted">Org</span>
          <select
            value={orgId === "" ? "" : String(orgId)}
            onChange={(e) => {
              const val = e.target.value;
              setOrgId(val === "" ? "" : Number(val));
              setOffset(0);
            }}
            aria-label="Filter by organization"
            className={`${input} max-w-[14rem]`}
          >
            <option value="">All</option>
            {orgOptions.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>

          <span className="ml-2 text-xs uppercase tracking-wider text-text-muted">Role</span>
          <button
            type="button"
            className={chipClass(role === "")}
            onClick={() => {
              setRole("");
              setOffset(0);
            }}
          >
            All
          </button>
          {ROLE_OPTIONS.map((r) => (
            <button
              key={r}
              type="button"
              className={chipClass(role === r)}
              onClick={() => {
                setRole(role === r ? "" : r);
                setOffset(0);
              }}
            >
              {r}
            </button>
          ))}

          <span className="ml-2 text-xs uppercase tracking-wider text-text-muted">Status</span>
          <button
            type="button"
            className={chipClass(status === "")}
            onClick={() => {
              setStatus("");
              setOffset(0);
            }}
          >
            All
          </button>
          {STATUS_OPTIONS.map((s) => (
            <button
              key={s}
              type="button"
              className={chipClass(status === s)}
              onClick={() => {
                setStatus(status === s ? "" : s);
                setOffset(0);
              }}
            >
              {s}
            </button>
          ))}

          {filtersActive && (
            <button
              type="button"
              onClick={resetFilters}
              className="ml-auto text-xs text-text-muted underline hover:text-text-primary"
            >
              Clear filters
            </button>
          )}
        </div>

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <SortableHeader
                  label="Name / email"
                  field="email"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Username"
                  field="username"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Org"
                  field="org_name"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Role"
                  field="role"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <th className="px-3 py-2 text-xs font-medium text-text-secondary">Status</th>
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
                    Loading
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-6 py-6 text-center text-text-muted">
                    No users match the current filters.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row) => {
                  const primaryOrg = row.orgs[0];
                  const statusLabel = row.is_superadmin
                    ? "superadmin"
                    : !row.is_active
                      ? "inactive"
                      : !row.email_verified
                        ? "unverified"
                        : "active";
                  return (
                    <tr key={row.id} className="border-b border-border-subtle">
                      <td className="px-6 py-3">
                        <Link
                          href={`/admin/users/${row.id}`}
                          className="text-accent hover:text-accent-hover"
                        >
                          {row.display_name || row.email}
                        </Link>
                        {row.display_name && (
                          <div className="text-xs text-text-muted">{row.email}</div>
                        )}
                      </td>
                      <td className="px-6 py-3 text-text-secondary">{row.username}</td>
                      <td className="px-6 py-3 text-text-secondary">
                        {primaryOrg ? (
                          <Link
                            href={`/admin/orgs/${primaryOrg.org_id}`}
                            className="hover:text-accent"
                          >
                            {primaryOrg.name}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="px-6 py-3 text-text-secondary">
                        {primaryOrg?.role ?? "—"}
                      </td>
                      <td className="px-6 py-3 text-text-secondary">{statusLabel}</td>
                      <td className="px-6 py-3 text-text-secondary tabular-nums">
                        {row.created_at?.slice(0, 10) ?? "—"}
                      </td>
                    </tr>
                  );
                })}
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
