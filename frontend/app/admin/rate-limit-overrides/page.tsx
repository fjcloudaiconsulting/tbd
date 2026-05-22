"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
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

const PAGE_SIZE = 50;

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
  patterns: string[];
  pre_auth_patterns: string[];
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
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<RateLimitOverrideListResponse | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [orgIdFilter, setOrgIdFilter] = useState("");
  const [userIdFilter, setUserIdFilter] = useState("");
  const [endpointFilter, setEndpointFilter] = useState("");
  const [offset, setOffset] = useState(0);

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
      limit: String(PAGE_SIZE),
      offset: String(offset),
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
  }, [canView, offset, orgIdFilter, userIdFilter, endpointFilter]);

  useEffect(() => {
    if (!authLoading && canView) {
      void refresh();
    }
  }, [authLoading, canView, refresh]);

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
                <th className="px-6 py-3">Endpoint</th>
                <th className="px-6 py-3">Limit</th>
                <th className="px-6 py-3">Period</th>
                <th className="px-6 py-3">Expires</th>
                <th className="px-6 py-3">Created</th>
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

        {data && data.total > PAGE_SIZE && (
          <div className="flex items-center justify-between px-6 py-3 text-xs text-text-muted">
            <span>
              {offset + 1}-{Math.min(offset + PAGE_SIZE, data.total)} of{" "}
              {data.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                className="rounded-md border border-border px-3 py-1 disabled:opacity-50"
              >
                Prev
              </button>
              <button
                type="button"
                disabled={offset + PAGE_SIZE >= data.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
                className="rounded-md border border-border px-3 py-1 disabled:opacity-50"
              >
                Next
              </button>
            </div>
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
                {catalogue?.patterns.map((p) => (
                  <option key={p} value={p}>
                    {p}
                    {catalogue.pre_auth_patterns.includes(p)
                      ? " (pre-auth, will no-op)"
                      : ""}
                  </option>
                ))}
              </select>
              {form.endpoint_pattern &&
                catalogue?.pre_auth_patterns.includes(
                  form.endpoint_pattern,
                ) && (
                  <p className="mt-1 text-xs text-text-muted">
                    This is a pre-auth endpoint. The override will be
                    saved but the resolver will fall back to the static
                    default at request time.
                  </p>
                )}
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
