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
import { isSuperadmin } from "@/lib/auth";
import {
  btnDanger,
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label,
  pageTitle,
} from "@/lib/styles";
import type {
  RateLimitOverride,
  RateLimitOverrideListResponse,
} from "@/lib/types";

// L4.10 — superadmin-only UI for per-org / per-user rate-limit
// overrides. The page is a thin shell over the admin REST endpoints
// at /api/v1/admin/rate-limit-overrides. Three sub-views:
//
// - The list (table with filters, paginated).
// - An add-or-edit modal driven by `editing` state. Null = closed,
//   "new" = create, RateLimitOverride = edit.
// - A delete confirm dialog driven by `deleting` state.
//
// Identity and scope semantics. Each row carries either an org_id
// OR a user_id (the backend enforces XOR). The form surfaces them
// as a single "scope" radio + numeric id input so the operator can
// only ever submit one. We do NOT support changing scope on edit;
// the backend rejects scope keys in the update payload, so the
// scope inputs are hidden / disabled in edit mode.

const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_SORT_BY = "created_at";
const DEFAULT_SORT_DIR: SortDir = "desc";

// Backend-whitelisted sort keys. Unknown keys 400, so we clamp a seeded
// URL value back to the default rather than send garbage.
const SORT_FIELDS = [
  "created_at",
  "endpoint_pattern",
  "max_requests",
  "period_seconds",
  "expires_at",
] as const;
type SortField = (typeof SORT_FIELDS)[number];

const PAGE_SIZE_VALUES = [10, 25, 50, 100] as const;

const dtFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function formatPeriod(seconds: number): string {
  if (seconds === 1) return "second";
  if (seconds === 60) return "minute";
  if (seconds === 3600) return "hour";
  if (seconds === 86400) return "day";
  return `${seconds}s`;
}

type FormState = {
  scope: "org" | "user";
  scopeId: string;
  endpoint_pattern: string;
  max_requests: string;
  period_seconds: string;
  expires_at: string;
  note: string;
};

type EndpointCatalogue = {
  // Patterns the backend will accept on create / update. Sorted.
  overridable: string[];
  // Patterns surfaced for context only. Rendered as disabled options
  // in the dropdown so operators can see the full decorator surface
  // and learn why those routes are not overridable. Sorted.
  pre_auth_informational: string[];
};

const EMPTY_FORM: FormState = {
  scope: "org",
  scopeId: "",
  endpoint_pattern: "",
  max_requests: "",
  period_seconds: "60",
  expires_at: "",
  note: "",
};

function rowToForm(row: RateLimitOverride): FormState {
  const isUser = row.user_id !== null;
  return {
    scope: isUser ? "user" : "org",
    scopeId: String(isUser ? row.user_id : row.org_id),
    endpoint_pattern: row.endpoint_pattern,
    max_requests: String(row.max_requests),
    period_seconds: String(row.period_seconds),
    expires_at: row.expires_at ? row.expires_at.slice(0, 16) : "",
    note: row.note ?? "",
  };
}

export default function AdminRateLimitOverridesPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner />
        </div>
      }
    >
      <AdminRateLimitOverridesPageContent />
    </Suspense>
  );
}

function AdminRateLimitOverridesPageContent() {
  const { user, loading: authLoading } = useAuth();
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

  const [data, setData] = useState<RateLimitOverrideListResponse | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [orgIdFilter, setOrgIdFilter] = useState("");
  const [userIdFilter, setUserIdFilter] = useState("");
  const [endpointFilter, setEndpointFilter] = useState("");
  const [offset, setOffset] = useState(initialOffset);
  const [sortBy, setSortBy] = useState<SortField>(initialSortBy);
  const [sortDir, setSortDir] = useState<SortDir>(initialSortDir);
  const [pageSize, setPageSize] = useState(initialPageSize);

  // Modal state. null = closed, "new" = create, row = edit.
  const [editing, setEditing] = useState<RateLimitOverride | "new" | null>(
    null,
  );
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const [deleting, setDeleting] = useState<RateLimitOverride | null>(null);
  const [deleteError, setDeleteError] = useState("");

  const [catalogue, setCatalogue] = useState<EndpointCatalogue | null>(null);

  // Gate: superadmin-only. Mirrors the announcements page guard
  // because there is no fine-grained role permission for rate-limit
  // overrides (architect-locked superadmin gate).
  const canView = user ? isSuperadmin(user) : false;

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canView) {
      router.replace("/dashboard");
    }
  }, [authLoading, user, canView, router]);

  const refresh = useCallback(async () => {
    if (!canView) return;
    setFetching(true);
    setError("");
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(offset),
      sort_by: sortBy,
      sort_dir: sortDir,
    });
    if (orgIdFilter.trim() && /^[1-9][0-9]*$/.test(orgIdFilter.trim())) {
      params.set("org_id", orgIdFilter.trim());
    }
    if (userIdFilter.trim() && /^[1-9][0-9]*$/.test(userIdFilter.trim())) {
      params.set("user_id", userIdFilter.trim());
    }
    if (endpointFilter.trim()) {
      params.set("endpoint_pattern", endpointFilter.trim());
    }
    try {
      const payload = await apiFetch<RateLimitOverrideListResponse>(
        `/api/v1/admin/rate-limit-overrides?${params.toString()}`,
      );
      setData(payload);
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load overrides"));
    } finally {
      setFetching(false);
    }
  }, [canView, offset, orgIdFilter, userIdFilter, endpointFilter, sortBy, sortDir, pageSize]);

  useEffect(() => {
    if (!authLoading && canView) {
      void refresh();
    }
  }, [authLoading, canView, refresh]);

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
    if (authLoading || !canView) return;
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
  }, [authLoading, canView, offset, sortBy, sortDir, pageSize, pathname, router]);

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

  // Fetch the endpoint catalogue once. The dropdown in the modal
  // sources its options from this; failure is non-fatal but the
  // form will be unusable until it loads (intentional — picking a
  // free-text pattern would silently no-op).
  useEffect(() => {
    if (!canView) return;
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiFetch<EndpointCatalogue>(
          "/api/v1/admin/rate-limit-overrides/endpoint-catalogue",
        );
        if (!cancelled) setCatalogue(payload);
      } catch {
        // Non-fatal: the list still loads. The dropdown will fall
        // back to an empty <option> set and the submit will surface
        // the schema's 422 with the catalogue in the error body.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [canView]);

  if (authLoading || !user || !canView) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  function openCreate() {
    setForm(EMPTY_FORM);
    setSubmitError("");
    setEditing("new");
  }

  function openEdit(row: RateLimitOverride) {
    setForm(rowToForm(row));
    setSubmitError("");
    setEditing(row);
  }

  function closeModal() {
    setEditing(null);
    setSubmitError("");
  }

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setSubmitting(true);
    setSubmitError("");
    try {
      if (!form.endpoint_pattern) {
        setSubmitError("Pick an endpoint from the catalogue.");
        setSubmitting(false);
        return;
      }
      const maxRequests = Number(form.max_requests);
      const periodSeconds = Number(form.period_seconds);
      if (!Number.isInteger(maxRequests) || maxRequests < 1) {
        setSubmitError("Max requests must be a positive integer.");
        setSubmitting(false);
        return;
      }
      if (!Number.isInteger(periodSeconds) || periodSeconds < 1) {
        setSubmitError("Period (seconds) must be a positive integer.");
        setSubmitting(false);
        return;
      }
      if (editing === "new") {
        const scopeId = Number(form.scopeId);
        if (!Number.isInteger(scopeId) || scopeId < 1) {
          setSubmitError("Scope id must be a positive integer.");
          setSubmitting(false);
          return;
        }
        const body: Record<string, unknown> = {
          endpoint_pattern: form.endpoint_pattern.trim(),
          max_requests: maxRequests,
          period_seconds: periodSeconds,
        };
        if (form.scope === "org") {
          body.org_id = scopeId;
        } else {
          body.user_id = scopeId;
        }
        if (form.expires_at) {
          body.expires_at = new Date(form.expires_at).toISOString();
        }
        if (form.note.trim()) {
          body.note = form.note.trim();
        }
        await apiFetch("/api/v1/admin/rate-limit-overrides", {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else if (editing && typeof editing === "object") {
        const body: Record<string, unknown> = {
          endpoint_pattern: form.endpoint_pattern.trim(),
          max_requests: maxRequests,
          period_seconds: periodSeconds,
        };
        // Send expires_at as ISO if set, or null to clear it. Note
        // is treated the same way so an operator can blank it.
        body.expires_at = form.expires_at
          ? new Date(form.expires_at).toISOString()
          : null;
        body.note = form.note.trim() === "" ? null : form.note.trim();
        await apiFetch(
          `/api/v1/admin/rate-limit-overrides/${editing.id}`,
          {
            method: "PATCH",
            body: JSON.stringify(body),
          },
        );
      }
      closeModal();
      await refresh();
    } catch (err) {
      setSubmitError(extractErrorMessage(err, "Failed to save override"));
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    setDeleteError("");
    try {
      await apiFetch(
        `/api/v1/admin/rate-limit-overrides/${deleting.id}`,
        { method: "DELETE" },
      );
      setDeleting(null);
      await refresh();
    } catch (err) {
      setDeleteError(extractErrorMessage(err, "Failed to delete override"));
    }
  }

  return (
    <AppShell>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className={pageTitle}>Rate-limit overrides</h1>
        <button
          type="button"
          onClick={openCreate}
          className={btnPrimary}
        >
          Add override
        </button>
      </div>

      <p className="mt-1 mb-2 text-sm text-text-muted">
        Bump or throttle an org or a user beyond the default per-route
        rate limits. User overrides win over org overrides.
      </p>

      <div
        role="note"
        aria-label="Pre-auth limitation"
        className="mb-4 rounded-md border border-border bg-warning-dim px-4 py-2 text-xs text-text-secondary"
      >
        Note: pre-auth endpoints (login, register, password-reset)
        cannot use per-org or per-user overrides. Adjust their static
        limits in code instead.
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Active overrides</h2>
        </div>

        <div className="grid grid-cols-1 gap-3 px-6 py-4 sm:grid-cols-3">
          <input
            type="text"
            inputMode="numeric"
            value={orgIdFilter}
            onChange={(e) => {
              setOffset(0);
              setOrgIdFilter(e.target.value);
            }}
            placeholder="Filter by org id"
            className={input}
            aria-label="Filter by org id"
          />
          <input
            type="text"
            inputMode="numeric"
            value={userIdFilter}
            onChange={(e) => {
              setOffset(0);
              setUserIdFilter(e.target.value);
            }}
            placeholder="Filter by user id"
            className={input}
            aria-label="Filter by user id"
          />
          <input
            type="search"
            value={endpointFilter}
            onChange={(e) => {
              setOffset(0);
              setEndpointFilter(e.target.value);
            }}
            placeholder="Endpoint pattern (exact)"
            className={input}
            aria-label="Filter by endpoint pattern"
          />
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="px-6 py-3">Scope</th>
                <SortableHeader
                  label="Endpoint"
                  field="endpoint_pattern"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Limit"
                  field="max_requests"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Period"
                  field="period_seconds"
                  activeField={sortBy}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Expires"
                  field="expires_at"
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
                <th className="px-6 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td colSpan={7} className="px-6 py-6 text-center text-text-muted">
                    Loading...
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-6 py-6 text-center text-text-muted">
                    No overrides match.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row) => (
                  <tr key={row.id} className="border-b border-border-subtle">
                    <td className="px-6 py-3 text-text-primary">
                      {row.user_id !== null ? (
                        <span>User #{row.user_id}</span>
                      ) : (
                        <span>Org #{row.org_id}</span>
                      )}
                    </td>
                    <td className="px-6 py-3 font-mono text-xs text-text-secondary">
                      {row.endpoint_pattern}
                    </td>
                    <td className="px-6 py-3 text-text-primary tabular-nums">
                      {row.max_requests}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {formatPeriod(row.period_seconds)}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.expires_at
                        ? dtFmt.format(new Date(row.expires_at))
                        : "-"}
                    </td>
                    <td className="px-6 py-3 text-text-muted tabular-nums">
                      {dtFmt.format(new Date(row.created_at))}
                    </td>
                    <td className="px-6 py-3 text-right">
                      <button
                        type="button"
                        onClick={() => openEdit(row)}
                        className={`${btnSecondary} mr-2`}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => setDeleting(row)}
                        className={btnDanger}
                      >
                        Delete
                      </button>
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

      {editing !== null && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="rate-limit-modal-title"
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
        >
          <form
            onSubmit={submit}
            className={`${card} w-full max-w-lg space-y-4 p-6`}
          >
            <h2 id="rate-limit-modal-title" className={cardTitle}>
              {editing === "new" ? "New override" : `Edit override #${editing.id}`}
            </h2>
            {submitError && (
              <div className={errorCls} role="alert">
                {submitError}
              </div>
            )}

            {editing === "new" && (
              <div>
                <label className={label}>Scope</label>
                <div className="flex gap-4 text-sm">
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="scope"
                      value="org"
                      checked={form.scope === "org"}
                      onChange={() =>
                        setForm({ ...form, scope: "org" })
                      }
                    />
                    Organization
                  </label>
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      name="scope"
                      value="user"
                      checked={form.scope === "user"}
                      onChange={() =>
                        setForm({ ...form, scope: "user" })
                      }
                    />
                    User
                  </label>
                </div>
                <div className="mt-2">
                  <label className={label} htmlFor="scope-id">
                    {form.scope === "org" ? "Org id" : "User id"}
                  </label>
                  <input
                    id="scope-id"
                    type="text"
                    inputMode="numeric"
                    value={form.scopeId}
                    onChange={(e) =>
                      setForm({ ...form, scopeId: e.target.value })
                    }
                    className={input}
                    required
                  />
                </div>
              </div>
            )}

            <div>
              <label className={label} htmlFor="endpoint-pattern">
                Endpoint pattern
              </label>
              <select
                id="endpoint-pattern"
                value={form.endpoint_pattern}
                onChange={(e) =>
                  setForm({ ...form, endpoint_pattern: e.target.value })
                }
                className={input}
                required
              >
                <option value="" disabled>
                  Select an endpoint
                </option>
                {catalogue?.overridable.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
                {catalogue?.pre_auth_informational.length ? (
                  <optgroup label="Pre-auth (not overridable)">
                    {catalogue.pre_auth_informational.map((p) => (
                      <option
                        key={p}
                        value={p}
                        disabled
                        title="Pre-auth route. Overrides are not honored; adjust slowapi default in code."
                      >
                        {p}
                      </option>
                    ))}
                  </optgroup>
                ) : null}
              </select>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={label} htmlFor="max-requests">
                  Max requests
                </label>
                <input
                  id="max-requests"
                  type="text"
                  inputMode="numeric"
                  value={form.max_requests}
                  onChange={(e) =>
                    setForm({ ...form, max_requests: e.target.value })
                  }
                  className={input}
                  required
                />
              </div>
              <div>
                <label className={label} htmlFor="period-seconds">
                  Period (seconds)
                </label>
                <input
                  id="period-seconds"
                  type="text"
                  inputMode="numeric"
                  value={form.period_seconds}
                  onChange={(e) =>
                    setForm({ ...form, period_seconds: e.target.value })
                  }
                  className={input}
                  required
                />
              </div>
            </div>

            <div>
              <label className={label} htmlFor="expires-at">
                Expires at (optional)
              </label>
              <input
                id="expires-at"
                type="datetime-local"
                value={form.expires_at}
                onChange={(e) =>
                  setForm({ ...form, expires_at: e.target.value })
                }
                className={input}
              />
            </div>

            <div>
              <label className={label} htmlFor="note">
                Note (optional)
              </label>
              <textarea
                id="note"
                value={form.note}
                onChange={(e) =>
                  setForm({ ...form, note: e.target.value })
                }
                rows={3}
                maxLength={5000}
                className={input}
              />
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={closeModal}
                className={btnSecondary}
                disabled={submitting}
              >
                Cancel
              </button>
              <button
                type="submit"
                className={btnPrimary}
                disabled={submitting}
              >
                {submitting ? "Saving..." : "Save"}
              </button>
            </div>
          </form>
        </div>
      )}

      {deleting && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="rate-limit-delete-title"
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
        >
          <div className={`${card} w-full max-w-md space-y-4 p-6`}>
            <h2 id="rate-limit-delete-title" className={cardTitle}>
              Delete override #{deleting.id}?
            </h2>
            <p className="text-sm text-text-secondary">
              This removes the override for{" "}
              <span className="font-mono">{deleting.endpoint_pattern}</span>{" "}
              on{" "}
              {deleting.user_id !== null
                ? `user #${deleting.user_id}`
                : `org #${deleting.org_id}`}
              . The default rate limit for that route will apply again.
            </p>
            {deleteError && (
              <div className={errorCls} role="alert">
                {deleteError}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleting(null)}
                className={btnSecondary}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmDelete}
                className={btnDanger}
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
