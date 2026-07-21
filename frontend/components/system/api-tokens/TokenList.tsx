"use client";

import type { ApiToken } from "@/lib/types";
import { badgeError, badgeNeutral, badgeSuccess } from "@/lib/styles";

import { expiryView, scopeLabel, shortDate } from "./expiry";

interface Props {
  tokens: ApiToken[];
  // Injected so status/expiry math is deterministic and pure in render.
  nowMs: number;
  onRevoke: (token: ApiToken) => void;
}

const STATUS_BADGE: Record<ApiToken["status"], string> = {
  active: badgeSuccess,
  expired: badgeError,
  revoked: badgeNeutral,
};

const STATUS_LABEL: Record<ApiToken["status"], string> = {
  active: "Active",
  expired: "Expired",
  revoked: "Revoked",
};

// Map the expiry tone to a design-token text color (No Off-Token — never a
// raw palette utility). `warning`/`danger` are the amber/red spec calls out.
const TONE_CLASS: Record<string, string> = {
  normal: "text-text-secondary",
  warning: "text-warning",
  danger: "text-danger",
};

const TH =
  "px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted";

export default function TokenList({ tokens, nowMs, onRevoke }: Props) {
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="border-b border-border text-left">
            <th className={TH}>Name</th>
            <th className={TH}>Prefix</th>
            <th className={TH}>Scope</th>
            <th className={TH}>Created</th>
            <th className={TH}>Expires</th>
            <th className={TH}>Last used</th>
            <th className={TH}>Status</th>
            <th className={TH}>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {tokens.length === 0 && (
            <tr>
              <td
                colSpan={8}
                className="px-4 py-8 text-center text-text-muted"
                data-testid="api-token-empty"
              >
                No API tokens yet. Mint one above to call the API without a
                short-lived session token.
              </td>
            </tr>
          )}
          {tokens.map((token) => {
            const expiry = expiryView(token.expires_at, nowMs);
            const canRevoke = token.status === "active";
            return (
              <tr
                key={token.id}
                className="border-b border-border"
                data-testid={`api-token-row-${token.id}`}
              >
                <td className="px-4 py-3 font-medium text-text-primary">
                  {token.name}
                </td>
                <td className="px-4 py-3">
                  <code className="rounded bg-surface-raised px-1.5 py-0.5 text-xs text-text-secondary">
                    {token.prefix}
                  </code>
                </td>
                <td className="px-4 py-3 text-text-secondary">
                  {scopeLabel(token.scope)}
                </td>
                <td className="px-4 py-3 text-text-muted">
                  {shortDate(token.created_at)}
                </td>
                <td
                  className={`px-4 py-3 ${TONE_CLASS[expiry.tone]}`}
                  data-testid={`api-token-expiry-${token.id}`}
                  data-tone={expiry.tone}
                >
                  {token.status === "revoked" ? "—" : expiry.label}
                </td>
                <td
                  className="px-4 py-3 text-text-muted"
                  data-testid={`api-token-lastused-${token.id}`}
                >
                  {shortDate(token.last_used_at)}
                </td>
                <td className="px-4 py-3">
                  <span
                    className={STATUS_BADGE[token.status]}
                    data-testid={`api-token-status-${token.id}`}
                  >
                    {STATUS_LABEL[token.status]}
                  </span>
                </td>
                <td className="px-4 py-3 text-right">
                  {canRevoke && (
                    <button
                      type="button"
                      onClick={() => onRevoke(token)}
                      className="text-xs text-text-muted hover:text-danger"
                      data-testid={`api-token-revoke-${token.id}`}
                    >
                      Revoke
                    </button>
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
