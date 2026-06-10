"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
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
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label as labelCls,
  pageTitle,
} from "@/lib/styles";
import type {
  PermissionCatalogResponse,
  RoleCreatePayload,
  RoleDetail,
  RoleListResponse,
  RoleSortField,
} from "@/lib/types";

const SLUG_PATTERN = /^[a-z][a-z0-9_]{2,63}$/;

const DEFAULT_PAGE_SIZE = 25;
// Default sort is the semantic frozen-first / name order, expressed by
// the backend when no explicit sort_by is sent. We surface that as the
// "unsorted" header state (no active column) so the page mirrors the
// server's default ordering until the user clicks a column.
const DEFAULT_SORT_DIR: SortDir = "asc";

// Backend-whitelisted sort keys. Unknown keys 400, so we clamp a seeded
// URL value back to the default (no active sort) rather than send garbage.
const SORT_FIELDS = [
  "name",
  "slug",
  "permission_count",
  "is_system_frozen",
] as const;

const PAGE_SIZE_VALUES = [10, 25, 50, 100] as const;

interface CreateModalProps {
  catalog: PermissionCatalogResponse;
  onClose: () => void;
  onCreated: () => void;
}

function CreateRoleModal({ catalog, onClose, onCreated }: CreateModalProps) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const slugValid = SLUG_PATTERN.test(slug);

  function togglePermission(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (!slugValid) {
      setErr(
        "Slug must start with a lowercase letter and contain only lowercase letters, digits, and underscores (3 to 64 chars).",
      );
      return;
    }
    if (!name.trim()) {
      setErr("Name is required.");
      return;
    }
    setSubmitting(true);
    try {
      const payload: RoleCreatePayload = {
        slug,
        name: name.trim(),
        description: description.trim() ? description.trim() : null,
        permissions: Array.from(selected).sort(),
      };
      await apiFetch<RoleDetail>("/api/v1/admin/roles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      onCreated();
    } catch (e) {
      setErr(extractErrorMessage(e, "Failed to create role"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-role-title"
    >
      <div className={`${card} w-full max-w-2xl max-h-[90vh] overflow-y-auto`}>
        <div className={cardHeader}>
          <h2 id="create-role-title" className={cardTitle}>
            New role
          </h2>
        </div>
        <form onSubmit={submit} className="space-y-4 px-6 py-4">
          {err && (
            <div className={errorCls} role="alert">
              {err}
            </div>
          )}
          <div>
            <label htmlFor="role-slug" className={labelCls}>
              Slug
            </label>
            <input
              id="role-slug"
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className={input}
              placeholder="support"
              autoComplete="off"
              required
            />
            <p className="mt-1 text-xs text-text-muted">
              Lowercase letters, digits, underscores. Must start with a letter.
            </p>
          </div>
          <div>
            <label htmlFor="role-name" className={labelCls}>
              Name
            </label>
            <input
              id="role-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={input}
              placeholder="Support"
              maxLength={120}
              required
            />
          </div>
          <div>
            <label htmlFor="role-description" className={labelCls}>
              Description (optional)
            </label>
            <textarea
              id="role-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={`${input} min-h-[5rem]`}
              maxLength={500}
              placeholder="What this role can do"
            />
          </div>
          <div>
            <p className={`${labelCls} mb-2`}>Permissions</p>
            <div className="space-y-3">
              {Object.entries(catalog.namespaces).map(([ns, keys]) => (
                <fieldset
                  key={ns}
                  className="rounded-md border border-border-subtle px-3 py-2"
                >
                  <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-text-muted">
                    {ns}
                  </legend>
                  <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                    {keys.map((key) => (
                      <label
                        key={key}
                        className="flex items-center gap-2 rounded px-1 py-1 text-sm text-text-primary hover:bg-surface-raised"
                      >
                        <input
                          type="checkbox"
                          checked={selected.has(key)}
                          onChange={() => togglePermission(key)}
                          className="h-4 w-4 rounded border-border accent-accent"
                        />
                        <span className="font-mono text-xs">{key}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              ))}
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className={btnSecondary}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className={btnPrimary}
              disabled={submitting || !slugValid || !name.trim()}
            >
              {submitting ? "Creating…" : "Create role"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function AdminRolesPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner />
        </div>
      }
    >
      <AdminRolesPageContent />
    </Suspense>
  );
}

// Admin roles list. URL-synced server-side sort + pagination mirrors
// /admin/orgs/page.tsx (the reference implementation). The query string
// is the source of truth for offset / sort / page_size; we seed React
// state from it on first render and mirror state back via router.replace
// so a refreshed or shared URL keeps the table state. The default order
// (no sort_by sent) is the backend's frozen-first / name ordering.
function AdminRolesPageContent() {
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
  // sortBy is null when no explicit sort is active (backend default order).
  const initialSortBy: RoleSortField | null = (() => {
    const raw = searchParams.get("sort_by");
    return raw && (SORT_FIELDS as readonly string[]).includes(raw)
      ? (raw as RoleSortField)
      : null;
  })();
  const initialSortDir: SortDir = (() => {
    const raw = searchParams.get("sort_dir");
    return raw === "asc" || raw === "desc" ? raw : DEFAULT_SORT_DIR;
  })();

  const [offset, setOffset] = useState(initialOffset);
  const [sortBy, setSortBy] = useState<RoleSortField | null>(initialSortBy);
  const [sortDir, setSortDir] = useState<SortDir>(initialSortDir);
  const [pageSize, setPageSize] = useState(initialPageSize);

  const [data, setData] = useState<RoleListResponse | null>(null);
  const [catalog, setCatalog] = useState<PermissionCatalogResponse | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [reloadCounter, setReloadCounter] = useState(0);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "roles.manage")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  // Load the permission catalog once (needed for the create modal). Kept
  // separate from the paginated roles fetch so paging doesn't re-pull it.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "roles.manage")) return;
    apiFetch<PermissionCatalogResponse>("/api/v1/admin/permissions")
      .then((perms) => setCatalog(perms))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")));
  }, [loading, user]);

  // Fetch a page of roles whenever sort / offset / pageSize changes (or a
  // create reloads the list). Only sends sort_by/sort_dir when a column is
  // active, so the default order stays the backend's frozen-first ordering.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "roles.manage")) return;
    setFetching(true);
    setError("");
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(offset),
    });
    if (sortBy) {
      params.set("sort_by", sortBy);
      params.set("sort_dir", sortDir);
    }
    apiFetch<RoleListResponse>(`/api/v1/admin/roles?${params.toString()}`)
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, offset, sortBy, sortDir, pageSize, reloadCounter]);

  // Clamp an over-offset URL back to the last valid page once data lands.
  useEffect(() => {
    if (!data) return;
    if (offset > 0 && offset >= data.total) {
      const lastOffset = Math.max(
        0,
        (pageCount(data.total, pageSize) - 1) * pageSize,
      );
      if (lastOffset !== offset) setOffset(lastOffset);
    }
  }, [data, offset, pageSize]);

  // Mirror state back to the URL (router.replace, scroll:false). Only
  // non-default params are written so a clean URL stays clean.
  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "roles.manage")) return;
    const params = new URLSearchParams();
    if (offset > 0) params.set("offset", String(offset));
    if (sortBy) {
      params.set("sort_by", sortBy);
      if (sortDir !== DEFAULT_SORT_DIR) params.set("sort_dir", sortDir);
    }
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
      const f = field as RoleSortField;
      setSortBy(f);
      setSortDir(f === sortBy ? (sortDir === "asc" ? "desc" : "asc") : "asc");
      setOffset(0);
    },
    [sortBy, sortDir],
  );

  const total = data?.total ?? 0;
  // SortableHeader wants a concrete active field; pass an unmatched value
  // when no column is active so no header shows a sort arrow.
  const activeField = sortBy ?? "";

  if (loading || !user || !hasPlatformPermission(user, "roles.manage")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-end justify-between gap-4">
        <h1 className={`${pageTitle} mb-0`}>Roles</h1>
        <button
          type="button"
          className={btnPrimary}
          onClick={() => setShowCreate(true)}
          disabled={!catalog}
        >
          + New role
        </button>
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Platform roles</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <SortableHeader
                  label="Name"
                  field="name"
                  activeField={activeField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Slug"
                  field="slug"
                  activeField={activeField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Permissions"
                  field="permission_count"
                  activeField={activeField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Type"
                  field="is_system_frozen"
                  activeField={activeField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <th className="px-6 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    No roles defined.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-border-subtle"
                  >
                    <td className="px-6 py-3">
                      <Link
                        href={`/admin/roles/${row.id}`}
                        className="text-accent hover:text-accent-hover"
                      >
                        {row.name}
                      </Link>
                      {row.description && (
                        <p className="mt-0.5 text-xs text-text-muted">
                          {row.description}
                        </p>
                      )}
                    </td>
                    <td className="px-6 py-3 font-mono text-xs text-text-secondary">
                      {row.slug}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.permission_count}
                    </td>
                    <td className="px-6 py-3">
                      {row.is_system_frozen ? (
                        <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-accent">
                          system
                        </span>
                      ) : (
                        <span className="text-xs text-text-muted">custom</span>
                      )}
                    </td>
                    <td className="px-6 py-3 text-right">
                      <Link
                        href={`/admin/roles/${row.id}`}
                        className="text-xs text-text-muted hover:text-accent"
                      >
                        View
                      </Link>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>

        {data && (total > pageSize || offset > 0) && (
          <div className="px-6">
            <Pagination
              page={Math.max(1, Math.floor(offset / pageSize) + 1)}
              pageSize={pageSize}
              total={total}
              onPageChange={(n) => setOffset((n - 1) * pageSize)}
              onPageSizeChange={(n) => {
                setPageSize(n);
                setOffset(0);
              }}
            />
          </div>
        )}
      </div>

      {showCreate && catalog && (
        <CreateRoleModal
          catalog={catalog}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            setReloadCounter((n) => n + 1);
          }}
        />
      )}
    </AppShell>
  );
}
