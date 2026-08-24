"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import AdminEmailChangeModal from "@/components/admin/AdminEmailChangeModal";
import AppShell from "@/components/AppShell";
import ConfirmModal from "@/components/ui/ConfirmModal";
import HelpAnchor from "@/components/HelpAnchor";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import {
  EMAIL_RECOVERY_NOTICE,
  EMAIL_RECOVERY_SSO_NOTICE,
} from "@/lib/email-recovery";
import {
  btnDangerSolid,
  btnSecondary,
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
// - Account recovery (users.reset_credentials only, TBD-362): repoint a
//   locked-out account's pending email claim, and cancel or resend a live
//   one. Deliberately NOT folded into the Danger zone: correcting a typo
//   and hard-deleting a user are different intents, and putting a routine
//   support action behind the red border trains operators to ignore it.
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
  // An UNPROVEN, operator- or self-asserted claim, never an identity. Null
  // when nothing is in flight. Rides on the shared admin list-and-detail
  // payload (`_serialize_user_row`), so it is present on both.
  pending_email: string | null;
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
  const [emailChangeOpen, setEmailChangeOpen] = useState(false);
  const [emailChangePrefill, setEmailChangePrefill] = useState("");
  const [recoveryError, setRecoveryError] = useState("");
  const [cancellingClaim, setCancellingClaim] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const canDeleteUsers = hasPlatformPermission(user, "users.delete");
  const canRecoverEmail = hasPlatformPermission(user, "users.reset_credentials");
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

  // Mirror of the POST handler's preconditions, in the SAME order it
  // evaluates them, so the operator is not walked into a guaranteed 409.
  // The server stays authoritative. `email_already_in_use` is absent on
  // purpose: it needs a cross-row lookup the browser cannot do, and it
  // surfaces as an inline error in the modal instead.
  //
  // ⚠ This gates the ACTION, not the card. The DELETE sibling deliberately
  // carries no precondition beyond existence -- clearing a claim is strictly
  // de-escalating and can only ever revoke a live promotion link -- so
  // hiding the whole card on a verified target would hide Cancel in exactly
  // the race (a target verified between trigger and redeem) where a live
  // claim most needs defusing.
  const recoveryBlockedReason = detail
    ? detail.email_verified
      ? "This account is already verified. Recovery is for accounts locked out before verification."
      : detail.is_superadmin
      ? "Platform superadmins cannot have their email repointed from this page."
      : !detail.is_active
      ? "This user is inactive. Reactivate them via the org members page first."
      : null
    : "Loading user…";

  async function handleCancelPendingEmail() {
    if (!detail) return;
    setRecoveryError("");
    setCancellingClaim(true);
    try {
      await apiFetch(`/api/v1/admin/users/${detail.id}/pending-email`, {
        method: "DELETE",
      });
      setReloadKey((k) => k + 1);
    } catch (err) {
      setRecoveryError(extractErrorMessage(err, "Could not cancel the pending change"));
    } finally {
      setCancellingClaim(false);
    }
  }

  async function handleDelete() {
    if (!detail) return;
    setDeleteError("");
    setDeleting(true);
    try {
      await apiFetch(`/api/v1/admin/users/${detail.id}`, { method: "DELETE" });
      setShowDeleteConfirm(false);
      router.replace("/admin/users");
    } catch (err) {
      // Architect feedback on PR #303: the error banner renders in the
      // danger-zone section (page body). If the modal stays mounted on
      // top of it, the operator may not see the failure. Close the
      // modal so the banner is visible.
      setShowDeleteConfirm(false);
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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- route-param validation guard inside the fetch effect; setState is intentional
      setError("Invalid user id");
      setFetching(false);
      return;
    }
    // Only the FIRST load swaps the page for a spinner. A revalidation
    // after a recovery write keeps the cards mounted and updates in place,
    // which is what "revalidates without a full reload" means here.
    if (reloadKey === 0) setFetching(true);
    apiFetch<UserDetail>(`/api/v1/admin/users/${userId}`)
      .then((d) => setDetail(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
    // `reloadKey` is bumped after a recovery write so the identity card and
    // the claim controls revalidate in place -- no full page reload.
  }, [loading, user, userId, reloadKey]);

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
              {/* ⚠ A LABELLED dt/dd pair, never a bare "Unverified" chip
                  beside an address: a badge that does not name its subject
                  reads as a contradiction next to the row above it. Sits
                  directly under "Email verified" so the two read as one
                  story -- not verified, and here is the address we are
                  waiting on. */}
              <div
                className="flex justify-between py-2"
                data-testid="identity-pending-email"
              >
                <dt className="text-text-muted">Pending email</dt>
                <dd>{detail.pending_email ?? "—"}</dd>
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

          {canRecoverEmail && (
            <section
              className={`${card} lg:col-span-2`}
              data-testid="account-recovery"
            >
              <div className={cardHeader}>
                <h2 className={cardTitle}>Account recovery</h2>
              </div>
              <div className="space-y-3 px-6 py-5">
                <p className="text-sm text-text-secondary">
                  Repoint the pending email claim for{" "}
                  <strong className="text-text-primary">{detail.email}</strong>{" "}
                  at a corrected address, for a user who mistyped it at signup
                  and can no longer receive their verification link.
                </p>
                {/* The ruling, rendered -- and on the CARD, not only in the
                    modal: the modal is seen after the operator has already
                    committed to acting, which is too late for a sentence
                    whose job is to correct the misunderstanding first. */}
                <p className="text-sm text-text-secondary">
                  {EMAIL_RECOVERY_NOTICE}
                </p>
                <p className="text-sm text-text-secondary">
                  {EMAIL_RECOVERY_SSO_NOTICE}
                </p>

                {recoveryError && (
                  <div className={errorCls} role="alert">
                    {recoveryError}
                  </div>
                )}

                {detail.pending_email && (
                  <div className="rounded-md border border-border-subtle bg-surface-raised px-4 py-3">
                    <p className="text-sm text-text-secondary">
                      A confirmation link is waiting at{" "}
                      <strong className="text-text-primary">
                        {detail.pending_email}
                      </strong>
                      . It expires 24 hours after it was sent.
                    </p>
                    <div className="mt-3 flex flex-col-reverse gap-2 sm:flex-row">
                      {/* ⚠ Cancel carries NO precondition beyond the claim
                          existing, matching the endpoint. Clearing a claim
                          only ever revokes a live promotion link, so the
                          operator must be able to defuse a mistargeted one
                          whatever state the target is in. */}
                      <button
                        type="button"
                        onClick={handleCancelPendingEmail}
                        disabled={cancellingClaim}
                        className={`${btnSecondary} w-full min-h-[44px] sm:w-auto`}
                      >
                        {cancellingClaim
                          ? "Cancelling…"
                          : "Cancel pending email change"}
                      </button>
                      {/* Resend is the same POST, so it reopens the same
                          form prefilled with the live claim -- the operator
                          supplies a fresh reason rather than the UI forging
                          the forensic note the audit row depends on. */}
                      <button
                        type="button"
                        disabled={recoveryBlockedReason !== null}
                        onClick={() => {
                          setRecoveryError("");
                          setEmailChangePrefill(detail.pending_email ?? "");
                          setEmailChangeOpen(true);
                        }}
                        className={`${btnSecondary} w-full min-h-[44px] sm:w-auto`}
                      >
                        Resend confirmation link
                      </button>
                    </div>
                  </div>
                )}

                <div>
                  <button
                    type="button"
                    disabled={recoveryBlockedReason !== null}
                    onClick={() => {
                      setRecoveryError("");
                      setEmailChangePrefill("");
                      setEmailChangeOpen(true);
                    }}
                    className={`${btnSecondary} min-h-[44px]`}
                  >
                    Change email address
                  </button>
                  {/* Disabled is paired with a VISIBLE reason, not a title
                      alone -- the convention the danger zone below sets,
                      because a tooltip is not reliably accessible. */}
                  {recoveryBlockedReason && (
                    <p
                      data-testid="recovery-blocked-reason"
                      className="mt-2 text-xs text-text-muted"
                    >
                      {recoveryBlockedReason}
                    </p>
                  )}
                </div>
              </div>
            </section>
          )}

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

      {emailChangeOpen && detail && (
        <AdminEmailChangeModal
          userId={detail.id}
          currentEmail={detail.email}
          emailVerified={detail.email_verified}
          initialEmail={emailChangePrefill}
          onClose={() => setEmailChangeOpen(false)}
          onChanged={() => setReloadKey((k) => k + 1)}
        />
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
