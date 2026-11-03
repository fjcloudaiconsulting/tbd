/**
 * TBD-664: how the sensitive tools' preview rows read. Balances and counters
 * by their signed movement, new rows by their value, and which row a change is
 * on when a preview touches several rows of one kind (a transfer's two legs).
 */
import {
  actionHeadline, changeLabel, changeValue, drillDown, isDelta, isNew, rowLabel,
} from "@/lib/agent/present";
import type { ActionChange } from "@/lib/agent/types";

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
});
