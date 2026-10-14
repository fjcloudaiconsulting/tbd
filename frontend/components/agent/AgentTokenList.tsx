"use client";

// The caller's agent tokens (TBD-581). Lowering access and revoking are the
// only edits: a token can never be raised, a new one is minted instead.

import { expiryView, scopeLabel, shortDate } from "@/components/system/api-tokens/expiry";
import { TONE_CLASS } from "@/components/system/api-tokens/TokenList";
import { lowerScopes } from "@/lib/agent/present";
import type { AgentToken } from "@/lib/agent/types";
import { badgeError, badgeNeutral, badgeSuccess, badgeWarning } from "@/lib/styles";

const STATUS: Record<AgentToken["status"], { label: string; cls: string }> = {
  active: { label: "Active", cls: badgeSuccess },
  expired: { label: "Expired", cls: badgeNeutral },
  revoked: { label: "Revoked", cls: badgeError },
  invalidated: { label: "Signed out", cls: badgeWarning },
};

const TH = "px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-secondary";
export const rowBtn =
  "inline-flex min-h-6 items-center rounded-md border border-border-strong bg-surface px-2.5 py-1 text-xs font-medium text-text-primary transition-colors hover:bg-surface-raised";

interface Props {
  tokens: AgentToken[];
  nowMs: number;
  // Absent on the org admin view: an admin may revoke a member's token, never lower it.
  onLower?: (t: AgentToken) => void;
  onRevoke: (t: AgentToken) => void;
  // The org admin view adds an Owner column.
  showOwner?: boolean;
  emptyText?: string;
}

export default function AgentTokenList({ tokens, nowMs, onLower, onRevoke, showOwner = false, emptyText }: Props) {
  const cols = showOwner ? 8 : 7;
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full min-w-[760px] text-sm">
        <thead>
          <tr className="border-b border-border text-left">
            {showOwner && <th className={TH}>Owner</th>}
            <th className={TH}>Name</th>
            <th className={TH}>Access</th>
            <th className={TH}>Created</th>
            <th className={TH}>Expires</th>
            <th className={TH}>Last used</th>
            <th className={TH}>Status</th>
            <th className={TH}><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {tokens.length === 0 && (
            <tr>
              <td colSpan={cols} className="px-4 py-8 text-center text-text-secondary">
                {emptyText ?? "No agent tokens yet. Create one above to connect an AI agent to your data."}
              </td>
            </tr>
          )}
          {tokens.map((t) => {
            const active = t.status === "active";
            const expiry = expiryView(t.expires_at, nowMs);
            return (
              <tr key={t.id} className="border-b border-border-subtle align-top" data-testid={`agent-token-${t.id}`}>
                {showOwner && (
                  <td className="px-4 py-3 text-text-primary [overflow-wrap:anywhere]">{"owner_email" in t ? t.owner_email : ""}</td>
                )}
                <td className="px-4 py-3">
                  <span className="block font-medium text-text-primary [overflow-wrap:anywhere]">{t.name}</span>
                  <code className="mt-1 inline-block rounded bg-surface-raised px-1.5 py-0.5 text-xs text-text-secondary">
                    {t.prefix}
                  </code>
                </td>
                <td className="px-4 py-3 text-text-primary">{scopeLabel(t.scope)}</td>
                <td className="px-4 py-3 text-text-secondary tabular-nums">{shortDate(t.created_at)}</td>
                <td className={`px-4 py-3 tabular-nums ${active ? TONE_CLASS[expiry.tone] : "text-text-secondary"}`}>
                  {active ? expiry.label : shortDate(t.expires_at)}
                </td>
                <td className="px-4 py-3 text-text-secondary tabular-nums">
                  {shortDate(t.last_used_at)}
                  {t.last_used_ip && <span className="block text-xs">{t.last_used_ip}</span>}
                </td>
                <td className="px-4 py-3">
                  <span className={STATUS[t.status].cls}>{STATUS[t.status].label}</span>
                </td>
                <td className="px-4 py-3">
                  {active && (
                    <div className="flex justify-end gap-2">
                      {onLower && lowerScopes(t.scope).length > 0 && (
                        <button type="button" className={rowBtn} onClick={() => onLower(t)}
                          aria-label={`Lower access for ${t.name}`}>
                          Lower access
                        </button>
                      )}
                      <button type="button" className={`${rowBtn} hover:text-danger`} onClick={() => onRevoke(t)}
                        aria-label={"owner_email" in t ? `Revoke ${t.name} owned by ${t.owner_email}` : `Revoke ${t.name}`}>
                        Revoke
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
