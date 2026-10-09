// Presentation of server-rendered agent data (TBD-581). Nothing here parses
// model text: every value shown comes from a server field, and every one goes
// through `text` so a {"untrusted": ...} wrapper renders as plain text.

import { scopeLabel } from "@/components/system/api-tokens/expiry";
import { formatMoney } from "@/lib/format";
import type { ActionChange, AgentScope, Wire } from "./types";

export function text(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object" && typeof (v as { untrusted?: unknown }).untrusted === "string") {
    return (v as { untrusted: string }).untrusted;
  }
  return typeof v === "object" ? "" : String(v);
}

const TOOL_LABELS: Record<string, string> = {
  accounts_list: "Looked up accounts",
  categories_list: "Looked up categories",
  budgets_list: "Looked up budgets",
  transactions_search: "Searched transactions",
  spending_by_category: "Summed spending by category",
  forecast_get: "Read the forecast",
  budgets_update_amount: "Change a budget amount",
  transactions_set_category: "Recategorize a transaction",
};

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? "Used a tool";
}

// Headline for a staged write. The server summary is only a fallback: it
// puts a currency AFTER the amount, and amounts here always lead with it.
export function actionHeadline(tool: string, summary: string, changes: ActionChange[]): string {
  return TOOL_LABELS[tool] ?? (changes.some((c) => c.currency) ? "Proposed change" : summary);
}

const FIELD_LABELS: Record<string, string> = {
  "budgets.amount": "Budget amount",
  "transactions.category_id": "Category",
  "category_rules.category_id": "Categorization rule",
};

export function changeLabel(c: ActionChange): string {
  return FIELD_LABELS[`${c.entity}.${c.field}`] ?? `${c.entity} ${c.field}`;
}

type Ctx = Record<string, unknown>;

// Category ids are named only where the server named them: the primary
// change's from/to in `context`. Any other id (a rule's current category)
// shows as "category #id", never a positional guess.
export function changeValue(
  c: ActionChange, side: "before" | "after", primary: ActionChange | undefined, ctx: Ctx,
): string {
  const v: Wire = c[side];
  if (v === null || v === undefined) return c.entity === "category_rules" ? "No rule" : "None";
  if (c.currency) return formatMoney(text(v), c.currency);
  if (c.field === "category_id") {
    const named = (k: "from" | "to") =>
      primary && primary.field === "category_id" && primary[k === "from" ? "before" : "after"] === v
        ? text((ctx[k] as Ctx | undefined)?.category_name)
        : "";
    const name = named("from") || named("to");
    return name || `category #${text(v)}`;
  }
  return text(v);
}

export function drillDown(c: ActionChange | undefined): { href: string; label: string } | null {
  if (!c) return null;
  if (c.entity === "transactions") {
    return { href: `/transactions?transaction_id=${encodeURIComponent(text(c.id))}`, label: "Open the transaction" };
  }
  if (c.entity === "budgets") return { href: "/budgets", label: "Open budgets" };
  return null;
}

export const SCOPE_RANK: Record<AgentScope, number> = { "agent:read": 0, "agent:write": 1, "agent:auto": 2 };

export const SCOPE_INFO: Record<AgentScope, { label: string; hint: string }> = {
  "agent:read": { label: scopeLabel("agent:read"), hint: "Reads accounts, budgets, transactions and the forecast. Changes nothing." },
  "agent:write": { label: scopeLabel("agent:write"), hint: "Can also stage changes. Each one waits for an explicit confirm." },
  "agent:auto": { label: scopeLabel("agent:auto"), hint: "Applies reversible changes without asking. You review and revert them here." },
};

export function lowerScopes(scope: AgentScope): AgentScope[] {
  return (Object.keys(SCOPE_RANK) as AgentScope[]).filter((s) => SCOPE_RANK[s] < SCOPE_RANK[scope]);
}

export const METER_LABELS: Record<string, string> = {
  "assistant.turns": "Assistant messages",
  "mcp.calls": "Connected agent calls",
  "platform_ai.tokens": "Platform AI tokens",
  "platform_ai.cents": "Platform AI spend",
};
