// Wire shapes of the agent endpoints (backend/app/routers/agent.py,
// agent_tokens.py, app/agent/actions.py). Every user-writable string the
// server returns may arrive wrapped as {"untrusted": "..."}.

export type Untrusted = { untrusted: string };
export type Wire = string | number | boolean | null | Untrusted;

export interface ActionChange {
  entity: string;
  id: Wire;
  field: string;
  before: Wire;
  after: Wire;
  currency?: string | null;
}

export interface StagedAction {
  action_id: string;
  summary: string;
  changes: ActionChange[];
  warnings: string[];
  context: Record<string, unknown>;
  expires_at: string;
  requires_confirmation: boolean;
}

export interface DriftRow {
  entity: string;
  id: Wire;
  field: string;
  expected: Wire;
  current: Wire;
}

export type ActionStatus = "pending" | "executing" | "done" | "failed" | "stale" | "cancelled";

export interface AgentActionRow {
  action_id: string;
  tool: string;
  channel: "in_app" | "mcp";
  risk: "read" | "write" | "sensitive";
  mode: "confirm" | "auto";
  status: ActionStatus;
  preview: { summary: string; changes: ActionChange[]; warnings: string[]; context: Record<string, unknown> };
  error_code: string | null;
  created_at: string;
  decided_at: string | null;
}

export type AgentScope = "agent:read" | "agent:write" | "agent:auto";

export interface AgentToken {
  id: number;
  name: string;
  prefix: string;
  scope: AgentScope;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  last_used_ip: string | null;
  status: "active" | "expired" | "revoked" | "invalidated";
}
