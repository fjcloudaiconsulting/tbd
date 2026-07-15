"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import { useTableState } from "@/lib/hooks/use-table-state";
import type { ListEnvelope } from "@/lib/types";
import {
  btnPrimary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label,
} from "@/lib/styles";

type MemberRole = "owner" | "admin" | "member";

type Member = {
  id: number;
  username: string;
  email: string;
  role: MemberRole;
  is_active: boolean;
};

type Invitation = {
  id: number;
  email: string;
  role: MemberRole;
  created_at: string;
  expires_at: string;
  inviter_username: string | null;
  status: "pending";
};

// Backend-whitelisted sort keys. Unknown keys 400, so each table sends
// only columns it exposes as headers.
const MEMBER_SORT_FIELDS = ["username", "email", "role"] as const;
type MemberSortField = (typeof MEMBER_SORT_FIELDS)[number];

const INVITATION_SORT_FIELDS = [
  "email",
  "role",
  "created_at",
  "expires_at",
] as const;
type InvitationSortField = (typeof INVITATION_SORT_FIELDS)[number];

export default function MembersSection({
  currentUserId,
  currentRole,
}: {
  currentUserId: number;
  currentRole: MemberRole;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [membersTotal, setMembersTotal] = useState(0);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [invitationsTotal, setInvitationsTotal] = useState(0);
  const [error, setError] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin">("member");
  const [inviting, setInviting] = useState(false);

  const isAdmin = currentRole === "owner" || currentRole === "admin";

  const membersTable = useTableState<MemberSortField>({
    key: "org-members",
    defaultSortField: "username",
    defaultSortDir: "asc",
    allowedSortFields: MEMBER_SORT_FIELDS,
  });
  const invitationsTable = useTableState<InvitationSortField>({
    key: "org-invitations",
    defaultSortField: "created_at",
    defaultSortDir: "desc",
    allowedSortFields: INVITATION_SORT_FIELDS,
  });

  const refresh = useCallback(async () => {
    try {
      const memberParams = new URLSearchParams({
        sort_by: membersTable.sortField,
        sort_dir: membersTable.sortDir,
        limit: String(membersTable.pageSize),
        offset: String((membersTable.page - 1) * membersTable.pageSize),
      });
      const inviteParams = new URLSearchParams({
        sort_by: invitationsTable.sortField,
        sort_dir: invitationsTable.sortDir,
        limit: String(invitationsTable.pageSize),
        offset: String((invitationsTable.page - 1) * invitationsTable.pageSize),
      });
      const [m, inv] = await Promise.all([
        apiFetch<ListEnvelope<Member>>(
          `/api/v1/orgs/members?${memberParams.toString()}`,
        ),
        isAdmin
          ? apiFetch<ListEnvelope<Invitation>>(
              `/api/v1/orgs/invitations?${inviteParams.toString()}`,
            )
          : Promise.resolve({ items: [], total: 0, limit: 0, offset: 0 }),
      ]);
      setMembers(m?.items ?? []);
      setMembersTotal(m?.total ?? 0);
      setInvitations(inv?.items ?? []);
      setInvitationsTotal(inv?.total ?? 0);
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load members"));
    }
  }, [
    isAdmin,
    membersTable.sortField,
    membersTable.sortDir,
    membersTable.page,
    membersTable.pageSize,
    invitationsTable.sortField,
    invitationsTable.sortDir,
    invitationsTable.page,
    invitationsTable.pageSize,
  ]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch: refresh() writes the members/invitations pages into state when sort/page change
    refresh().catch(() => {});
  }, [refresh]);

  const handleMemberSort = useCallback(
    (field: string) => {
      if (!(MEMBER_SORT_FIELDS as readonly string[]).includes(field)) return;
      const f = field as MemberSortField;
      membersTable.setSort(
        f,
        f === membersTable.sortField && membersTable.sortDir === "asc"
          ? "desc"
          : "asc",
      );
    },
    [membersTable],
  );

  const handleInvitationSort = useCallback(
    (field: string) => {
      if (!(INVITATION_SORT_FIELDS as readonly string[]).includes(field)) return;
      const f = field as InvitationSortField;
      invitationsTable.setSort(
        f,
        f === invitationsTable.sortField && invitationsTable.sortDir === "asc"
          ? "desc"
          : "asc",
      );
    },
    [invitationsTable],
  );

  async function handleInvite(e: FormEvent) {
    e.preventDefault();
    setError("");
    setInviting(true);
    try {
      await apiFetch("/api/v1/orgs/invitations", {
        method: "POST",
        body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
      });
      setInviteEmail("");
      setInviteRole("member");
      await refresh();
    } catch (err) {
      setError(extractErrorMessage(err, "Could not send invitation"));
    } finally {
      setInviting(false);
    }
  }

  async function handleRevoke(id: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/orgs/invitations/${id}`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(extractErrorMessage(err, "Could not revoke invitation"));
    }
  }

  async function handleRemove(userId: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/orgs/members/${userId}`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(extractErrorMessage(err, "Could not remove member"));
    }
  }

  return (
    <section className={card}>
      <header className={cardHeader}>
        <h2 className={cardTitle}>Members</h2>
      </header>
      <div className="px-6 py-5 space-y-6">
      {error && (
        <div className={errorCls} role="alert">
          {error}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-text-muted">
              <SortableHeader
                label="Username"
                field="username"
                activeField={membersTable.sortField}
                dir={membersTable.sortDir}
                onSort={handleMemberSort}
              />
              <SortableHeader
                label="Email"
                field="email"
                activeField={membersTable.sortField}
                dir={membersTable.sortDir}
                onSort={handleMemberSort}
              />
              <SortableHeader
                label="Role"
                field="role"
                activeField={membersTable.sortField}
                dir={membersTable.sortDir}
                onSort={handleMemberSort}
              />
              {isAdmin && <th className="py-2" />}
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const canRemove =
                isAdmin && m.id !== currentUserId && !(currentRole !== "owner" && m.role === "owner");
              return (
                <tr key={m.id} className="border-b border-border-subtle">
                  <td className="px-3 py-2 text-text-primary">{m.username}</td>
                  <td className="px-3 py-2 text-text-secondary">{m.email}</td>
                  <td className="px-3 py-2 text-text-secondary">{m.role}</td>
                  {isAdmin && (
                    <td className="px-3 py-2 text-right">
                      {canRemove && (
                        <button
                          type="button"
                          onClick={() => handleRemove(m.id)}
                          aria-label={`Remove ${m.username}`}
                          className="text-xs text-text-muted hover:text-danger"
                        >
                          Remove
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
        {(membersTotal > membersTable.pageSize || membersTable.page > 1) && (
          <Pagination
            page={membersTable.page}
            pageSize={membersTable.pageSize}
            total={membersTotal}
            onPageChange={membersTable.setPage}
            onPageSizeChange={membersTable.setPageSize}
          />
        )}
      </div>

      {isAdmin && (
        <>
          <h3 className="text-sm font-semibold text-text-primary">
            Pending invitations
          </h3>
          {invitations.length === 0 ? (
            <p className="text-sm text-text-muted">No pending invitations.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-text-muted">
                    <SortableHeader
                      label="Email"
                      field="email"
                      activeField={invitationsTable.sortField}
                      dir={invitationsTable.sortDir}
                      onSort={handleInvitationSort}
                    />
                    <SortableHeader
                      label="Role"
                      field="role"
                      activeField={invitationsTable.sortField}
                      dir={invitationsTable.sortDir}
                      onSort={handleInvitationSort}
                    />
                    <SortableHeader
                      label="Expires"
                      field="expires_at"
                      activeField={invitationsTable.sortField}
                      dir={invitationsTable.sortDir}
                      onSort={handleInvitationSort}
                    />
                    <th className="py-2" />
                  </tr>
                </thead>
                <tbody>
                  {invitations.map((inv) => (
                    <tr key={inv.id} className="border-b border-border-subtle">
                      <td className="px-3 py-2 text-text-secondary">{inv.email}</td>
                      <td className="px-3 py-2 text-text-secondary">{inv.role}</td>
                      <td className="px-3 py-2 text-text-secondary">
                        {inv.expires_at ? inv.expires_at.slice(0, 10) : "(none)"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          onClick={() => handleRevoke(inv.id)}
                          aria-label={`Revoke invitation for ${inv.email}`}
                          className="text-xs text-text-muted hover:text-danger"
                        >
                          Revoke
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(invitationsTotal > invitationsTable.pageSize ||
            invitationsTable.page > 1) && (
            <Pagination
              page={invitationsTable.page}
              pageSize={invitationsTable.pageSize}
              total={invitationsTotal}
              onPageChange={invitationsTable.setPage}
              onPageSizeChange={invitationsTable.setPageSize}
            />
          )}

          <form
            onSubmit={handleInvite}
            className="flex flex-col gap-3 sm:flex-row sm:items-end"
          >
            <div className="flex-1">
              <label htmlFor="invite-email" className={label}>
                Invite by email
              </label>
              <input
                id="invite-email"
                type="email"
                required
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                className={input}
                placeholder="teammate@example.com"
              />
            </div>
            <div>
              <label htmlFor="invite-role" className={label}>
                Role
              </label>
              <select
                id="invite-role"
                value={inviteRole}
                onChange={(e) =>
                  setInviteRole(e.target.value as "member" | "admin")
                }
                className={input}
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <button
              type="submit"
              disabled={inviting}
              className={btnPrimary}
            >
              {inviting ? "Sending..." : "Send invitation"}
            </button>
          </form>
        </>
      )}
      </div>
    </section>
  );
}
