/**
 * TBD-664: how the sensitive tools' preview rows read. Balances and counters
 * by their signed movement, new rows by their value, and which row a change is
 * on when a preview touches several rows of one kind (a transfer's two legs).
 */
import {
  actionHeadline, changeLabel, changeValue, drillDown, isDelta, isNew, rowLabel, summaryChange,
  summaryLabel, summaryTotal, toolLabel,
} from "@/lib/agent/present";
import type { ActionChange, ActionSummary } from "@/lib/agent/types";
import { BALANCE_MASK, setBalancesHidden } from "@/lib/format";

const c = (over: Partial<ActionChange>): ActionChange => ({
  entity: "transactions", id: 7, field: "amount", before: null, after: null, ...over,
});

describe("present (TBD-664)", () => {
  it("a balance moves by a signed amount, never shown as a value", () => {
    const out = c({ entity: "accounts", id: 3, field: "balance_delta", after: "-15.01", currency: "EUR" });
    expect(isDelta(out)).toBe(true);
    expect(changeLabel(out)).toBe("Balance");
    expect(changeValue(out, "after", undefined, {})).toBe("−€15.01");
    expect(changeValue({ ...out, after: "4.50" }, "after", undefined, {})).toBe("+€4.50");
    const counter = c({ entity: "import_batches", id: 2, field: "pending_count_delta", after: "-1" });
    expect(changeLabel(counter)).toBe("Import rows to review");
    expect(changeValue(counter, "after", undefined, {})).toBe("−1");
  });

  it("a row a change would create has no link yet", () => {
    expect(drillDown(c({ id: "new:1" }))).toBeNull();
    expect(drillDown(c({ id: 7 }))?.href).toBe("/transactions?transaction_id=7");
  });

  it("names rows: new legs, accounts and tag links", () => {
    expect(isNew("new:2")).toBe(true);
    expect(isNew(12)).toBe(false);
    expect(rowLabel(c({ id: "new:2" }))).toBe("New transaction 2");
    expect(rowLabel(c({ entity: "accounts", id: 3 }))).toBe("Account #3");
    expect(rowLabel(c({ entity: "transaction_tags", id: [7, 4], field: "tag_id" }))).toBe("Transaction #7");
  });

  it("ids read as references, a new leg's link as the other new transaction", () => {
    expect(changeValue(c({ field: "linked_transaction_id", after: "new:2" }), "after", undefined, {}))
      .toBe("the other new transaction");
    expect(changeValue(c({ field: "linked_transaction_id", before: 9 }), "before", undefined, {}))
      .toBe("transaction #9");
    expect(changeValue(c({ field: "account_id", after: 3 }), "after", undefined, {})).toBe("account #3");
  });

  it("TBD-669: report layouts read by their size, the tools have headlines", () => {
    const layout = { version: 1, widgets: [{ id: "k1" }, { id: "k2" }] } as unknown as ActionChange["after"];
    const ch = c({ entity: "reports", id: 4, field: "layout_json", before: {} as never, after: layout });
    expect(changeLabel(ch)).toBe("Layout");
    expect(changeValue(ch, "after", undefined, {})).toBe("2 widgets");
    expect(changeValue(ch, "before", undefined, {})).toBe("0 widgets");
    expect(changeValue(c({ field: "canvas_filters_json", after: {} as never }), "after", undefined, {}))
      .toBe("No filters");
    expect(rowLabel(c({ entity: "report_versions", id: "new:1" }))).toBe("New saved version 1");
    expect(drillDown(ch)).toEqual({ href: "/reports/4", label: "Open the report" });
    expect(actionHeadline("reports_update_layout", "s", [])).toBe("Change a report's layout or sharing");
    expect(toolLabel("reports_query")).toBe("Ran a report query");
  });

  it("TBD-787: a notification toggle reads On or Off", () => {
    const ch = c({ entity: "user_notification_preferences", id: 2, field: "email_org_activity",
      before: true, after: false });
    expect(changeLabel(ch)).toBe("Organization activity emails");
    expect([changeValue(ch, "before", undefined, {}), changeValue(ch, "after", undefined, {})])
      .toEqual(["On", "Off"]);
    expect(actionHeadline("notification_preferences_update", "s", [])).toBe("Change notification preferences");
  });

  it("TBD-788: loading a domain's tools reads like a lookup", () => {
    expect(toolLabel("load_tools")).toBe("Got the tools for a change");
  });

  it("the four tools have headlines", () => {
    expect(actionHeadline("transactions_update_money", "s", [])).toBe("Change a transaction's amount or status");
    expect(actionHeadline("transactions_create", "s", [])).toBe("Add a transaction");
    expect(actionHeadline("transactions_delete", "s", [])).toBe("Delete a transaction");
    expect(actionHeadline("transactions_transfer", "s", [])).toBe("Move money between accounts");
  });

  it("the recurring and billing-period tools read by their rows (TBD-667)", () => {
    expect(actionHeadline("recurring_stop", "s", [])).toBe("Stop a recurring transaction");
    expect(actionHeadline("billing_period_close", "s", [])).toBe("Close the billing period");
    expect(actionHeadline("recurring_update_money", "s", [])).toBe("Change a recurring transaction's amount or schedule");
    const left = c({ field: "recurring_id", before: 4, after: null });
    expect(changeLabel(left)).toBe("Recurring series");
    expect(changeValue(left, "before", undefined, {})).toBe("recurring transaction #4");
    const frontier = c({ entity: "recurring_transactions", id: 4, field: "next_due_date" });
    expect(changeLabel(frontier)).toBe("Next due date");
    expect(changeLabel(c({ entity: "recurring_transactions", field: "to_account_id" }))).toBe("Destination account");
    expect(rowLabel(frontier)).toBe("Recurring transaction #4");
    expect(drillDown(frontier)?.href).toBe("/recurring");
    expect(rowLabel(c({ entity: "billing_periods", id: "new:2", field: "start_date" }))).toBe("New billing period 2");
  });

  it("the account tools (TBD-665) have headlines and name their rows", () => {
    expect(actionHeadline("accounts_adjust_balance", "s", [])).toBe("Adjust an account's balance");
    expect(actionHeadline("cycle_payments_upsert", "s", [])).toBe("Set a card payment");
    const pay = c({ entity: "cc_cycle_payments", id: "new:1", field: "amount", after: "80.00", currency: "EUR" });
    expect(changeLabel(pay)).toBe("Card payment");
    expect(rowLabel(pay)).toBe("New card payment 1");
    expect(changeValue(pay, "after", undefined, {})).toBe("€80.00");
    expect(changeLabel(c({ entity: "accounts", field: "is_default" }))).toBe("Default account");
    expect(actionHeadline("accounts_update_money", "s", [])).toBe("Change an account's opening balance or type");
    expect(changeLabel(c({ entity: "accounts", field: "principal_amount" }))).toBe("Loan principal");
  });

  it("TBD-666: budget and category rows read by their kind, a new master by its number", () => {
    expect(actionHeadline("budgets_create", "s", [])).toBe("Create a budget");
    expect(actionHeadline("categories_restore_recommended", "s", [])).toBe("Restore recommended categories");
    expect(rowLabel(c({ entity: "budgets", id: "new:2" }))).toBe("New budget 2");
    expect(rowLabel(c({ entity: "billing_periods", id: "new:1" }))).toBe("New billing period 1");
    expect(changeLabel(c({ entity: "categories", field: "parent_id" }))).toBe("Master category");
    expect(changeValue(c({ entity: "categories", field: "parent_id", after: "new:3" }), "after", undefined, {}))
      .toBe("new category 3");
    expect(changeValue(c({ entity: "categories", field: "parent_id", before: 4 }), "before", undefined, {}))
      .toBe("category #4");
  });

  it("TBD-668: forecast plan and Plans rows read by their kind, a new plan by name", () => {
    expect(actionHeadline("forecast_plan_populate", "s", [])).toBe("Fill the forecast plan from your history");
    expect(actionHeadline("scenarios_delete", "s", [])).toBe("Delete a plan");
    expect(toolLabel("scenarios_simulate")).toBe("Projected a plan");
    const item = c({ entity: "forecast_plan_items", id: "new:2", field: "plan_id", after: "new:1" });
    expect(rowLabel(item)).toBe("New forecast item 2");
    expect(changeValue(item, "after", undefined, {})).toBe("the new forecast plan");
    expect(changeValue(c({ entity: "forecast_plan_items", field: "plan_id", after: 7 }), "after", undefined, {}))
      .toBe("forecast plan #7");
    expect(changeLabel(c({ entity: "forecast_plans", field: "status" }))).toBe("Plan status");
    expect(rowLabel(c({ entity: "scenarios", id: 3 }))).toBe("Plan #3");
    expect(changeLabel(c({ entity: "plan_goals", field: "name" }))).toBe("Goal");
  });

  it("a summarised group reads by the stated count, its one change and its total (TBD-786)", () => {
    const s = (over: Partial<ActionSummary>): ActionSummary => ({
      entity: "transactions", change: "update", count: 412, sample: [], totals: {}, ...over,
    });
    const left = s({ field: "recurring_id", before: 4, after: null });
    expect(summaryLabel(left)).toBe("412 transactions changed");
    const one = summaryChange(left)!;
    expect(changeLabel(one)).toBe("Recurring series");
    expect(changeValue(one, "before", undefined, {})).toBe("recurring transaction #4");
    expect(summaryLabel(s({ change: "delete", count: 40 }))).toBe("40 transactions deleted");
    expect(summaryChange(s({ change: "delete" }))).toBeNull();
    const added = s({ entity: "categories", change: "insert", count: 80 });
    expect(summaryLabel(added)).toBe("80 categories added");
    expect(summaryTotal(added)).toBe("");
    expect(summaryTotal(s({ totals: { amount: "630.00", currency: "EUR" } }))).toBe("€630.00");
    // no stated currency (mixed or missing): no total, never a bare unmasked number
    expect(summaryTotal(s({ totals: { planned_amount: "12.00" } }))).toBe("");
    setBalancesHidden(true);
    try {
      const masked = summaryTotal(s({ totals: { amount: "630.00", currency: "EUR" } }));
      expect(masked).toContain(BALANCE_MASK);
      expect(masked).not.toContain("630");
    } finally {
      setBalancesHidden(false);
    }
  });
});
