"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
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

export default function MembersSection({
  currentUserId,
  currentRole,
}: {
  currentUserId: number;
  currentRole: MemberRole;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [error, setError] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin">("member");
  const [inviting, setInviting] = useState(false);

  const isAdmin = currentRole === "owner" || currentRole === "admin";

  const refresh = useCallback(async () => {
    try {
      const [m, inv] = await Promise.all([
        apiFetch<Member[]>("/api/v1/orgs/members"),
        isAdmin
          ? apiFetch<Invitation[]>("/api/v1/orgs/invitations")
          : Promise.resolve([] as Invitation[]),
      ]);
      setMembers(m ?? []);
      setInvitations(inv ?? []);
    } catch (err) {
      setError(extractErrorMessage(err, "Failed to load members"));
    }
  }, [isAdmin]);

  useEffect(() => {
    refresh().catch(() => {});
  }, [refresh]);

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
              <th className="py-2 pr-4">Username</th>
              <th className="py-2 pr-4">Email</th>
              <th className="py-2 pr-4">Role</th>
              {isAdmin && <th className="py-2" />}
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const canRemove =
                isAdmin && m.id !== currentUserId && !(currentRole !== "owner" && m.role === "owner");
              return (
                <tr key={m.id} className="border-b border-border-subtle">
                  <td className="py-2 pr-4 text-text-primary">{m.username}</td>
                  <td className="py-2 pr-4 text-text-secondary">{m.email}</td>
                  <td className="py-2 pr-4 text-text-secondary">{m.role}</td>
                  {isAdmin && (
                    <td className="py-2 text-right">
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
      </div>

      {isAdmin && (
        <>
          <h3 className="text-sm font-semibold text-text-primary">
            Pending invitations
          </h3>
          {invitations.length === 0 ? (
            <p className="text-sm text-text-muted">No pending invitations.</p>
          ) : (
            <ul className="divide-y divide-border-subtle">
              {invitations.map((inv) => (
                <li
                  key={inv.id}
                  className="flex items-center justify-between py-2 text-sm"
                >
                  <span className="text-text-secondary">
                    {inv.email} <span className="text-text-muted">— {inv.role}</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => handleRevoke(inv.id)}
                    aria-label={`Revoke invitation for ${inv.email}`}
                    className="text-xs text-text-muted hover:text-danger"
                  >
                    Revoke
                  </button>
                </li>
              ))}
            </ul>
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
