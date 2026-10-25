"use client";

// Changes waiting for the person's confirmation (TBD-664). A connected agent
// (MCP) can stage a sensitive change (money, a new or deleted transaction)
// but never apply it: the server refuses its confirm. It lands here, and only
// an explicit Apply in this browser runs it. The assistant's own proposals
// show here too, so nothing staged is out of sight. Nothing pending, nothing
// shown.

import useSWR, { useSWRConfig } from "swr";

import { ApiResponseError, apiFetch } from "@/lib/api";
import type { AgentActionRow, StagedAction } from "@/lib/agent/types";
import { card, cardHeader, cardTitle } from "@/lib/styles";

import PreviewCard from "./PreviewCard";

export const PENDING_KEY = "/api/v1/agent/actions?status=pending&limit=20";

function staged(r: AgentActionRow): StagedAction {
  return {
    action_id: r.action_id, summary: r.preview.summary, changes: r.preview.changes,
    warnings: r.preview.warnings, context: r.preview.context ?? {}, expires_at: r.expires_at,
    requires_confirmation: true,
  };
}

// Who proposed it: a revert is the person's own; an MCP row names its token.
function origin(r: AgentActionRow): string {
  if (r.preview.context?.reverts) return "Your revert";
  if (r.channel === "mcp") return r.token_name ? `From your agent ${r.token_name}` : "From a connected agent";
  return "From the assistant";
}

// The activity list shows what was applied: refresh it after a decision.
const isActivity = (k: unknown) => typeof k === "string" && k.startsWith("/api/v1/agent/actions?status=done");

export default function PendingActions() {
  const { mutate: mutateAll } = useSWRConfig();
  const { data, error } = useSWR<{ items: AgentActionRow[] }>(PENDING_KEY, (u: string) => apiFetch(u), {
    shouldRetryOnError: false,
  });

  // No `ai.agent` on the plan: the list is closed (403), like the activity.
  if (error instanceof ApiResponseError && error.status === 403) return null;
  if (error) {
    return <p role="alert" className="mb-6 text-sm text-danger">Changes waiting for you could not be loaded.</p>;
  }
  // A decided card keeps its outcome until the list next loads.
  const rows = (data?.items ?? []).filter((r) => r.live);
  if (rows.length === 0) return null;

  return (
    <section className={`${card} mb-6`} aria-labelledby="pending-title">
      <div className={cardHeader}>
        <h2 id="pending-title" className={cardTitle}>Waiting for your confirmation</h2>
      </div>
      <p className="px-6 pt-4 text-sm text-text-secondary">
        Proposed by the assistant or by your connected agents. A connected agent can propose a change to
        money, or a new or deleted transaction, but only you can apply it, here.
      </p>
      <ul className="space-y-4 px-6 pb-6 pt-4">
        {rows.map((r, i) => (
          <li key={r.action_id}>
            <p className="mb-1 text-xs text-text-secondary [overflow-wrap:anywhere]">{origin(r)}</p>
            <PreviewCard
              tool={r.tool}
              action={staged(r)}
              emphasis={i === 0}
              onDecided={() => void mutateAll(isActivity)}
            />
          </li>
        ))}
      </ul>
    </section>
  );
}
