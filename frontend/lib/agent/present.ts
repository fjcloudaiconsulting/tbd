// Presentation of server-rendered agent data (TBD-581). Nothing here parses
// model text: every value shown comes from a server field, and every one goes
// through `text` so a {"untrusted": ...} wrapper renders as plain text.

import { scopeLabel } from "@/components/system/api-tokens/expiry";
import { formatMoney } from "@/lib/format";
import type { ActionChange, ActionSummary, AgentScope, Wire } from "./types";

// The backend serializes naive UTC datetimes without an offset.
export function utcDate(iso: string): Date {
  return new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
}

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
  transactions_get: "Read a transaction",
  transactions_update: "Edit a transaction",
  transactions_update_money: "Change a transaction's amount or status",
  transactions_create: "Add a transaction",
  transactions_delete: "Delete a transaction",
  transactions_transfer: "Move money between accounts",
  recurring_list: "Looked up recurring transactions",
  recurring_create: "Add a recurring transaction",
  recurring_update: "Edit a recurring transaction",
  recurring_update_money: "Change a recurring transaction's amount or schedule",
  recurring_stop: "Stop a recurring transaction",
  recurring_delete: "Delete a recurring transaction",
  recurring_skip_next: "Skip the next occurrence",
  recurring_materialise_next: "Write the next occurrence now",
  recurring_generate: "Generate the recurring transactions due",
  billing_cycle_get: "Read the billing cycle day",
  billing_periods_list: "Looked up billing periods",
  billing_cycle_update: "Change the billing cycle day",
  billing_period_create: "Add a billing period",
  billing_periods_ensure_future: "Add the upcoming billing periods",
  billing_period_close: "Close the billing period",
  accounts_get: "Read an account",
  account_types_list: "Looked up account types",
  cycle_payments_list: "Read a card's upcoming payments",
  accounts_create: "Add an account",
  accounts_update: "Edit an account",
  accounts_update_money: "Change an account's opening balance or type",
  accounts_delete: "Delete an account",
  accounts_adjust_balance: "Adjust an account's balance",
  account_types_create: "Add an account type",
  account_types_update: "Rename an account type",
  account_types_delete: "Delete an account type",
  cycle_payments_create: "Set a card payment",
  cycle_payments_upsert: "Set a card payment",
  cycle_payments_delete: "Remove a card payment",
  budgets_create: "Create a budget",
  budgets_from_forecast: "Create budgets from the forecast",
  budgets_copy_from_period: "Copy budgets from another period",
  budgets_draft_next: "Draft next period's budgets",
  budgets_rebalance: "Rebalance budgets",
  budgets_delete: "Delete a budget",
  categories_create: "Create a category",
  categories_update: "Edit a category",
  categories_move: "Move a subcategory",
  categories_batch_move: "Move subcategories",
  categories_delete: "Delete a category",
  categories_restore_recommended: "Restore recommended categories",
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
  "budgets.category_id": "Category",
  "budgets.period_start": "Period start",
  "billing_periods.start_date": "Period start",
  "billing_periods.end_date": "Period end",
  "categories.type": "Type",
  "categories.description": "Description",
  "categories.parent_id": "Master category",
  "categories.slug": "Recommended as",
  "categories.is_system": "Recommended",
  "recurring_transactions.category_id": "Category",
  "forecast_plan_items.category_id": "Category",
  "forecast_plan_items.planned_amount": "Planned amount",
  "transactions.category_id": "Category",
  "transactions.description": "Description",
  "transactions.date": "Date",
  "transactions.settled_date": "Settlement date",
  "transactions.amount": "Amount",
  "transactions.account_id": "Account",
  "transactions.type": "Type",
  "transactions.status": "Status",
  "transactions.linked_transaction_id": "Linked to",
  "transactions.reconciliation_state": "Import review",
  "transaction_tags.tag_id": "Tag",
  "category_rules.category_id": "Categorization rule",
  "accounts.balance_delta": "Balance",
  "accounts.balance": "Balance",
  "accounts.name": "Name",
  "accounts.account_type_id": "Account type",
  "accounts.currency": "Currency",
  "accounts.opening_balance": "Opening balance",
  "accounts.opening_balance_date": "Opening balance date",
  "accounts.is_active": "Active",
  "accounts.is_default": "Default account",
  "accounts.payment_source_account_id": "Paid from",
  "accounts.close_day": "Statement close day",
  "accounts.credit_limit": "Credit limit",
  "accounts.payment_day": "Card payment day",
  "accounts.payment_day_relative_month": "Payment months after close",
  "accounts.apr": "APR",
  "accounts.payment_strategy": "Payment strategy",
  "accounts.fixed_payment_amount": "Fixed payment",
  "accounts.principal_amount": "Loan principal",
  "accounts.interest_rate_apr": "Loan interest rate",
  "accounts.term_months": "Loan term (months)",
  "accounts.origination_date": "Loan start date",
  "accounts.first_payment_date": "First loan payment",
  "account_types.name": "Account type name",
  "cc_cycle_payments.amount": "Card payment",
  "cc_cycle_payments.period_anchor_year": "Cycle year",
  "cc_cycle_payments.period_anchor_month": "Cycle month",
  "cc_cycle_payments.account_id": "Card account",
  "categories.name": "Category name",
  "import_batches.row_count_delta": "Import rows",
  "import_batches.accepted_count_delta": "Accepted import rows",
  "import_batches.pending_count_delta": "Import rows to review",
  "import_batches.status": "Import",
  "transactions.recurring_id": "Recurring series",
  "recurring_transactions.account_id": "Account",
  "recurring_transactions.description": "Description",
  "recurring_transactions.amount": "Amount",
  "recurring_transactions.type": "Type",
  "recurring_transactions.frequency": "Frequency",
  "recurring_transactions.next_due_date": "Next due date",
  "recurring_transactions.auto_settle": "Settle automatically",
  "recurring_transactions.is_active": "Active",
  "recurring_transactions.occurrence_count": "Number of occurrences",
  "recurring_transactions.occurrences_elapsed": "Occurrences so far",
  "budgets.period_end": "Budget period end",
  "organizations.billing_cycle_day": "Billing cycle day",
};

export function changeLabel(c: ActionChange): string {
  return FIELD_LABELS[`${c.entity}.${c.field}`] ?? `${c.entity} ${c.field}`;
}

// A row a staged write inserts is named "new:<n>" until it exists.
export function isNew(id: unknown): boolean {
  return text(id).startsWith("new:");
}

const ROW_NOUN: Record<string, string> = {
  transactions: "Transaction", accounts: "Account", import_batches: "Import",
  recurring_transactions: "Recurring transaction", billing_periods: "Billing period", budgets: "Budget",
  account_types: "Account type", cc_cycle_payments: "Card payment", categories: "Category",
  forecast_plan_items: "Forecast item",
};

// Which row a change is on, for a preview that touches several rows of one
// kind (the two legs of a transfer, the accounts whose balances move).
export function rowLabel(c: ActionChange): string {
  if (c.entity === "transaction_tags" && Array.isArray(c.id)) return `Transaction #${text(c.id[0])}`;
  const noun = ROW_NOUN[c.entity] ?? c.entity;
  return isNew(c.id) ? `New ${noun.toLowerCase()} ${text(c.id).slice(4)}` : `${noun} #${text(c.id)}`;
}

const PLURAL: Record<string, string> = {
  transactions: "transactions", recurring_transactions: "recurring transactions",
  forecast_plan_items: "forecast items", category_rules: "categorization rules",
  categories: "categories", budgets: "budgets", billing_periods: "billing periods",
};

// A summarised group (TBD-786) reads by the count the server stated.
export function summaryLabel(s: ActionSummary): string {
  const noun = PLURAL[s.entity] ?? s.entity;
  if (s.change === "insert") return `${s.count} ${noun} added`;
  if (s.change === "delete") return `${s.count} ${noun} deleted`;
  return `${s.count} ${noun} changed`;
}

// The group's one changed field, as a change row the card renders.
export function summaryChange(s: ActionSummary): ActionChange | null {
  if (s.change !== "update" || !s.field) return null;
  return { entity: s.entity, id: null, field: s.field, before: s.before ?? null, after: s.after ?? null };
}

export function summaryTotal(s: ActionSummary): string {
  const v = s.totals.amount ?? s.totals.planned_amount;
  if (v === undefined) return "";
  return s.totals.currency ? formatMoney(v, s.totals.currency) : v;
}

// A counter or a balance is named by how much it moves, never by its value.
export function isDelta(c: ActionChange): boolean {
  return c.field.endsWith("_delta");
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
  if (isDelta(c)) {
    const n = Number(text(v));
    const amount = c.currency ? formatMoney(String(Math.abs(n)), c.currency) : String(Math.abs(n));
    return `${n < 0 ? "−" : "+"}${amount}`;
  }
  if (c.currency) return formatMoney(text(v), c.currency);
  if (c.field === "linked_transaction_id") return isNew(v) ? "the other new transaction" : `transaction #${text(v)}`;
  if (c.field === "account_id") return `account #${text(v)}`;
  if (c.field === "recurring_id") return `recurring transaction #${text(v)}`;
  if (c.field === "tag_id") return `tag #${text(v)}`;
  if (c.field === "parent_id") return isNew(v) ? `new category ${text(v).slice(4)}` : `category #${text(v)}`;
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
  if (!c || isNew(c.id)) return null; // a row the change would create does not exist yet
  if (c.entity === "transactions") {
    return { href: `/transactions?transaction_id=${encodeURIComponent(text(c.id))}`, label: "Open the transaction" };
  }
  if (c.entity === "budgets") return { href: "/budgets", label: "Open budgets" };
  if (c.entity === "recurring_transactions") return { href: "/recurring", label: "Open recurring" };
  if (c.entity === "billing_periods") return { href: "/settings/billing", label: "Open billing periods" };
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
  "platform_ai.micros": "Platform AI spend",
};
