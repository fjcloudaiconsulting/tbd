"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import ConfirmModal from "@/components/ui/ConfirmModal";
import HelpAnchor from "@/components/HelpAnchor";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  btnDangerSolid,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
} from "@/lib/styles";

// Detail view for a single user. Cards:
// - Identity: id, email, username, name, flags.
// - Org memberships: list with link to each /admin/orgs/[id].
// - Recent audit events: last 10 events authored by this user.
// - Danger zone (users.delete only): hard-delete the User row when
//   the target is deactivated, non-superadmin, and not the actor.
//   The server still enforces every precondition; the disabled
//   button + tooltip is UX only.

type OrgRef = {
  org_id: number;
  name: string;
  role: string;
};

type AuditEventRow = {
  id: number;
  event_type: string;
  outcome: string;
  target_org_id: number | null;
  target_org_name: string | null;
  created_at: string | null;
};

type UserDetail = {
  id: number;
  email: string;
  username: string;
  display_name: string | null;
  is_superadmin: boolean;
  is_active: boolean;
  email_verified: boolean;
  mfa_enabled: boolean;
  password_set: boolean;
  password_changed_at: string | null;
  sessions_invalidated_at: string | null;
  onboarded_at: string | null;
  created_at: string | null;
  phone: string | null;
  orgs: OrgRef[];
  recent_audit_events: AuditEventRow[];
};

function YesNo({ value }: { value: boolean }) {
  return (
    <span className={value ? "text-success" : "text-text-muted"}>
      {value ? "Yes" : "No"}
    </span>
  );
}

export default function AdminUserDetailPage() {
  const params = useParams();
  const userId = Number(params?.user_id);
  const { user, loading } = useAuth();
  const router = useRouter();
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const canDeleteUsers = hasPlatformPermission(user, "users.delete");
  // Mirror the backend preconditions so the disabled tooltip
  // explains WHY the action is unavailable. Server stays
  // authoritative; this just stops the operator from clicking
  // through a guaranteed 409.
  const deleteBlockedReason = detail
    ? detail.is_superadmin
      ? "Platform superadmins cannot be deleted via this page."
      : detail.id === user?.id
      ? "You cannot delete your own user."
      : detail.is_active
      ? "Deactivate the user first via the org members page."
      : null
    : "Loading user…";

  async function handleDelete() {
    if (!detail) return;
    setDeleteError("");
    setDeleting(true);
    try {
      await apiFetch(`/api/v1/admin/users/${detail.id}`, { method: "DELETE" });
      setShowDeleteConfirm(false);
      router.replace("/admin/users");
    } catch (err) {
      setDeleteError(extractErrorMessage(err, "Delete failed"));
    } finally {
      setDeleting(false);
    }
  }

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

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "users.view")) return;
    if (!Number.isFinite(userId)) {
      setError("Invalid user id");
      setFetching(false);
      return;
    }
    setFetching(true);
    apiFetch<UserDetail>(`/api/v1/admin/users/${userId}`)
      .then((d) => setDetail(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, userId]);

  if (loading || !user || !hasPlatformPermission(user, "users.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex items-start gap-2">
          <h1 className={`${pageTitle} mb-0`}>
            {detail?.display_name || detail?.email || "User"}
          </h1>
          <HelpAnchor section="admin-users" label="User detail" variant="inline-title" />
        </div>
        <Link
          href="/admin/users"
          className="text-sm text-text-muted hover:text-accent"
        >
          Back to users
        </Link>
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      {fetching && (
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      )}

      {!fetching && detail && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Identity</h2>
            </div>
            <dl className="divide-y divide-border-subtle px-6 py-2 text-sm">
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">User ID</dt>
                <dd className="tabular-nums">{detail.id}</dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Email</dt>
                <dd>{detail.email}</dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Username</dt>
                <dd>{detail.username}</dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Display name</dt>
                <dd>{detail.display_name ?? "—"}</dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Active</dt>
                <dd>
                  <YesNo value={detail.is_active} />
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Email verified</dt>
                <dd>
                  <YesNo value={detail.email_verified} />
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Superadmin</dt>
                <dd>
                  <YesNo value={detail.is_superadmin} />
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">MFA enabled</dt>
                <dd>
                  <YesNo value={detail.mfa_enabled} />
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Password set</dt>
                <dd>
                  <YesNo value={detail.password_set} />
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Created</dt>
                <dd className="tabular-nums">
                  {detail.created_at?.slice(0, 10) ?? "—"}
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Onboarded</dt>
                <dd className="tabular-nums">
                  {detail.onboarded_at?.slice(0, 10) ?? "—"}
                </dd>
              </div>
              <div className="flex justify-between py-2">
                <dt className="text-text-muted">Password last changed</dt>
                <dd className="tabular-nums">
                  {detail.password_changed_at?.slice(0, 10) ?? "—"}
                </dd>
              </div>
            </dl>
          </div>

          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Organization memberships</h2>
            </div>
            <div className="px-6 py-4 text-sm">
              {detail.orgs.length === 0 && (
                <p className="text-text-muted">No org memberships.</p>
              )}
              {detail.orgs.length > 0 && (
                <ul className="space-y-2">
                  {detail.orgs.map((org) => (
                    <li
                      key={org.org_id}
                      className="flex items-center justify-between rounded-md border border-border-subtle px-3 py-2"
                    >
                      <Link
                        href={`/admin/orgs/${org.org_id}`}
                        className="text-accent hover:text-accent-hover"
                      >
                        {org.name}
                      </Link>
                      <span className="text-xs uppercase tracking-wider text-text-muted">
                        {org.role}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className={`${card} lg:col-span-2`}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Recent audit events</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                    <th className="px-6 py-3">When</th>
                    <th className="px-6 py-3">Event</th>
                    <th className="px-6 py-3">Outcome</th>
                    <th className="px-6 py-3">Target org</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.recent_audit_events.length === 0 && (
                    <tr>
                      <td colSpan={4} className="px-6 py-6 text-center text-text-muted">
                        No recent audit events authored by this user.
                      </td>
                    </tr>
                  )}
                  {detail.recent_audit_events.map((ev) => (
                    <tr key={ev.id} className="border-b border-border-subtle">
                      <td className="px-6 py-3 text-text-secondary tabular-nums">
                        {ev.created_at?.replace("T", " ").slice(0, 19) ?? "—"}
                      </td>
                      <td className="px-6 py-3 font-mono text-xs text-text-secondary">
                        {ev.event_type}
                      </td>
                      <td className="px-6 py-3 text-text-secondary">{ev.outcome}</td>
                      <td className="px-6 py-3 text-text-secondary">
                        {ev.target_org_id ? (
                          <Link
                            href={`/admin/orgs/${ev.target_org_id}`}
                            className="hover:text-accent"
                          >
                            {ev.target_org_name ?? `Org ${ev.target_org_id}`}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {canDeleteUsers && (
            <section
              className={`${card} border-danger/40 lg:col-span-2`}
              data-testid="user-danger-zone"
            >
              <div className={cardHeader}>
                <h2 className={`${cardTitle} text-danger`}>Danger zone</h2>
              </div>
              <div className="space-y-3 px-6 py-5">
                <p className="text-sm text-text-secondary">
                  Permanently delete{" "}
                  <strong className="text-text-primary">
                    {detail.email}
                  </strong>
                  . Their User row is removed. Audit events authored by this
                  user are preserved (the actor field becomes blank but the
                  snapshot email stays). This action cannot be undone.
                </p>
                {deleteError && (
                  <div className={errorCls} role="alert">
                    {deleteError}
                  </div>
                )}
                <div>
                  <button
                    type="button"
                    disabled={deleteBlockedReason !== null || deleting}
                    onClick={() => setShowDeleteConfirm(true)}
                    className={btnDangerSolid}
                    title={
                      deleteBlockedReason
                        ?? "Permanently delete this user."
                    }
                    aria-label={`Delete user ${detail.email}`}
                  >
                    Delete user
                  </button>
                  {deleteBlockedReason && (
                    <p className="mt-2 text-xs text-text-muted">
                      {deleteBlockedReason}
                    </p>
                  )}
                </div>
              </div>
            </section>
          )}
        </div>
      )}

      <ConfirmModal
        open={showDeleteConfirm}
        title="Delete user"
        message={
          detail
            ? `Permanently delete ${detail.email}? This removes the User row. The action cannot be undone.`
            : ""
        }
        confirmLabel={deleting ? "Deleting…" : "Delete user"}
        cancelLabel="Cancel"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </AppShell>
  );
}
