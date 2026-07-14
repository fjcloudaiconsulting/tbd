import { describe, expect, it } from "vitest";

import { describeWidgetFilters } from "@/lib/reports/describe-filters";
import { buildPresetRanges } from "@/lib/reports/date-presets";
import type { BarWidget, WidgetFilters } from "@/lib/reports/types";
import type { Account, Category } from "@/lib/types";

const NOW = new Date(2026, 5, 15); // 2026-06-15 (stable)

function bar(filters: WidgetFilters): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      filters,
    },
  };
}

const ACCTS: Account[] = [
  {
    id: 1,
    name: "Checking",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 0,
    currency: "USD",
    is_active: true,
    close_day: null,
    is_default: true,
  },
  {
    id: 2,
    name: "Savings",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 0,
    currency: "USD",
    is_active: true,
    close_day: null,
    is_default: false,
  },
  {
    id: 3,
    name: "Credit",
    account_type_id: 2,
    account_type_name: "Card",
    account_type_slug: "card",
    balance: 0,
    currency: "USD",
    is_active: true,
    close_day: null,
    is_default: false,
  },
];

const CATS: Category[] = [
  {
    id: 10,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 20,
    name: "Transport",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "transport",
    is_system: false,
    transaction_count: 0,
  },
];

const NO_LOOKUPS = { accounts: [], categories: [] };

describe("describeWidgetFilters", () => {
  it("returns [] for a widget with no set filters", () => {
    expect(describeWidgetFilters(bar({}), {}, NO_LOOKUPS, NOW)).toEqual([]);
  });

  it("emits a date chip inherited from canvas (not overridden), preset-named", () => {
    const presets = buildPresetRanges(NOW);
    const chips = describeWidgetFilters(
      bar({}),
      { date_range: presets.this_month },
      NO_LOOKUPS,
      NOW,
    );
    const date = chips.find((c) => c.key === "date");
    expect(date).toBeDefined();
    expect(date?.overridden).toBeFalsy();
    expect(date?.label).toBe("This month");
  });

  it("marks the date chip overridden when the widget date differs from canvas", () => {
    const presets = buildPresetRanges(NOW);
    const chips = describeWidgetFilters(
      bar({ date_range: { start: "2026-03-01", end: "2026-03-31" } }),
      { date_range: presets.this_month },
      NO_LOOKUPS,
      NOW,
    );
    const date = chips.find((c) => c.key === "date");
    expect(date?.overridden).toBe(true);
  });

  it("does NOT emit a date chip when neither widget nor canvas has a date", () => {
    const chips = describeWidgetFilters(bar({}), {}, NO_LOOKUPS, NOW);
    expect(chips.find((c) => c.key === "date")).toBeUndefined();
  });

  it("omits the date chip for a date-less source even with an effective date", () => {
    const presets = buildPresetRanges(NOW);
    // Same inputs as the inherited-date case, but the source doesn't
    // support a date filter (accounts) → no date chip at all.
    const chips = describeWidgetFilters(
      bar({}),
      { date_range: presets.this_month },
      NO_LOOKUPS,
      NOW,
      false,
    );
    expect(chips.find((c) => c.key === "date")).toBeUndefined();
  });

  it("keeps non-date chips when the source is date-less", () => {
    // A date-less source still surfaces account/category/txn_type chips —
    // only the date chip is suppressed.
    const chips = describeWidgetFilters(
      bar({ txn_type: ["expense"] }),
      { date_range: buildPresetRanges(NOW).this_month },
      NO_LOOKUPS,
      NOW,
      false,
    );
    expect(chips.find((c) => c.key === "date")).toBeUndefined();
    expect(chips.find((c) => c.key === "txn_type")).toBeDefined();
  });

  it("emits a Settled status chip when status is set", () => {
    const chips = describeWidgetFilters(
      bar({ status: "settled" }),
      {},
      NO_LOOKUPS,
      NOW,
    );
    expect(chips.find((c) => c.key === "status")?.label).toBe("Settled");
  });

  it("emits a Pending status chip when status is set", () => {
    const chips = describeWidgetFilters(
      bar({ status: "pending" }),
      {},
      NO_LOOKUPS,
      NOW,
    );
    expect(chips.find((c) => c.key === "status")?.label).toBe("Pending");
  });

  it("emits no status chip when status is unset", () => {
    const chips = describeWidgetFilters(bar({}), {}, NO_LOOKUPS, NOW);
    expect(chips.find((c) => c.key === "status")).toBeUndefined();
  });

  it("emits a status chip INHERITED from canvas (not overridden)", () => {
    const chips = describeWidgetFilters(
      bar({}),
      { status: "settled" },
      NO_LOOKUPS,
      NOW,
    );
    const status = chips.find((c) => c.key === "status");
    expect(status?.label).toBe("Settled");
    expect(status?.overridden).toBeFalsy();
  });

  it("marks the status chip overridden when the widget status differs from canvas", () => {
    const chips = describeWidgetFilters(
      bar({ status: "pending" }),
      { status: "settled" },
      NO_LOOKUPS,
      NOW,
    );
    const status = chips.find((c) => c.key === "status");
    expect(status?.label).toBe("Pending");
    expect(status?.overridden).toBe(true);
  });

  it("omits the status chip for a status-less source (sourceSupportsStatus=false)", () => {
    // A cascaded canvas status must not surface a chip on a source that
    // can't honor it — the resolver drops it at query time.
    const chips = describeWidgetFilters(
      bar({}),
      { status: "settled" },
      NO_LOOKUPS,
      NOW,
      true, // sourceSupportsDate
      false, // sourceSupportsStatus
    );
    expect(chips.find((c) => c.key === "status")).toBeUndefined();
  });

  it("resolves account ids to names with +N truncation", () => {
    const chips = describeWidgetFilters(
      bar({ account_ids: [1, 2, 3] }),
      {},
      { accounts: ACCTS, categories: [] },
      NOW,
    );
    expect(chips.find((c) => c.key === "accounts")?.label).toBe("Checking +2");
  });

  it("falls back to a count label when no account name resolves", () => {
    const chips = describeWidgetFilters(
      bar({ account_ids: [99, 98] }),
      {},
      { accounts: ACCTS, categories: [] },
      NOW,
    );
    expect(chips.find((c) => c.key === "accounts")?.label).toBe("2 accounts");
  });

  it("uses the singular count noun when a single account id is unresolved", () => {
    const chips = describeWidgetFilters(
      bar({ account_ids: [99] }),
      {},
      { accounts: ACCTS, categories: [] },
      NOW,
    );
    expect(chips.find((c) => c.key === "accounts")?.label).toBe("1 account");
  });

  it("counts unresolved ids in +N when SOME names resolve (no underreport)", () => {
    // ids [1, 99]: only id 1 ("Checking") resolves. The widget still
    // filters on 2 accounts, so the chip must read "Checking +1" — NOT a
    // bare "Checking" that hides the second (unresolved) id.
    const chips = describeWidgetFilters(
      bar({ account_ids: [1, 99] }),
      {},
      { accounts: ACCTS, categories: [] },
      NOW,
    );
    expect(chips.find((c) => c.key === "accounts")?.label).toBe("Checking +1");
  });

  it("counts MULTIPLE unresolved ids in +N alongside a resolved name", () => {
    // ids [1, 98, 99]: only id 1 resolves; "+2" must cover both unresolved.
    const chips = describeWidgetFilters(
      bar({ account_ids: [1, 98, 99] }),
      {},
      { accounts: ACCTS, categories: [] },
      NOW,
    );
    expect(chips.find((c) => c.key === "accounts")?.label).toBe("Checking +2");
  });

  it("counts a leading-unresolved id in +N (resolved name need not be first)", () => {
    // ids [99, 1]: id 99 is unresolved, id 1 ("Checking") resolves second.
    // The shown name is the first RESOLVED one, and +N still counts all
    // other ids → "Checking +1".
    const chips = describeWidgetFilters(
      bar({ account_ids: [99, 1] }),
      {},
      { accounts: ACCTS, categories: [] },
      NOW,
    );
    expect(chips.find((c) => c.key === "accounts")?.label).toBe("Checking +1");
  });

  it("resolves category ids to names", () => {
    const chips = describeWidgetFilters(
      bar({ category_ids: [10, 20] }),
      {},
      { accounts: [], categories: CATS },
      NOW,
    );
    expect(chips.find((c) => c.key === "categories")?.label).toBe("Groceries +1");
  });

  it("emits a txn_type chip joining the selected types, only when set", () => {
    expect(
      describeWidgetFilters(bar({ txn_type: ["expense"] }), {}, NO_LOOKUPS, NOW)
        .find((c) => c.key === "txn_type")?.label,
    ).toBe("Expense");
    expect(
      describeWidgetFilters(
        bar({ txn_type: ["income", "expense"] }),
        {},
        NO_LOOKUPS,
        NOW,
      ).find((c) => c.key === "txn_type")?.label,
    ).toBe("Income, Expense");
    // Legacy reports persisted a single string — coerced + still rendered.
    expect(
      describeWidgetFilters(
        bar({ txn_type: "income" } as unknown as WidgetFilters),
        {},
        NO_LOOKUPS,
        NOW,
      ).find((c) => c.key === "txn_type")?.label,
    ).toBe("Income");
    expect(
      describeWidgetFilters(bar({}), {}, NO_LOOKUPS, NOW).find(
        (c) => c.key === "txn_type",
      ),
    ).toBeUndefined();
  });

  it("emits amount chips for range / one-sided bounds", () => {
    expect(
      describeWidgetFilters(
        bar({ amount_range: { min: 100, max: 500 } }),
        {},
        NO_LOOKUPS,
        NOW,
      ).find((c) => c.key === "amount")?.label,
    ).toBe("$100 to $500");
    expect(
      describeWidgetFilters(
        bar({ amount_range: { min: 100 } }),
        {},
        NO_LOOKUPS,
        NOW,
      ).find((c) => c.key === "amount")?.label,
    ).toBe("≥ $100");
    expect(
      describeWidgetFilters(
        bar({ amount_range: { max: 500 } }),
        {},
        NO_LOOKUPS,
        NOW,
      ).find((c) => c.key === "amount")?.label,
    ).toBe("≤ $500");
  });

  it("emits a tags chip and adds the (any) suffix only for tag_match=any", () => {
    const anyChips = describeWidgetFilters(
      bar({ tag_names: ["a", "b"], tag_match: "any" }),
      {},
      NO_LOOKUPS,
      NOW,
    );
    expect(anyChips.find((c) => c.key === "tags")?.label).toContain("(any)");

    const allChips = describeWidgetFilters(
      bar({ tag_names: ["a", "b"], tag_match: "all" }),
      {},
      NO_LOOKUPS,
      NOW,
    );
    expect(allChips.find((c) => c.key === "tags")?.label).not.toContain("(any)");
  });

  it("orders chips date, txn_type, amount, tags, accounts, categories", () => {
    const presets = buildPresetRanges(NOW);
    const chips = describeWidgetFilters(
      bar({
        date_range: presets.this_month,
        txn_type: ["income"],
        amount_range: { min: 10 },
        tag_names: ["x"],
        account_ids: [1],
        category_ids: [10],
      }),
      {},
      { accounts: ACCTS, categories: CATS },
      NOW,
    );
    expect(chips.map((c) => c.key)).toEqual([
      "date",
      "txn_type",
      "amount",
      "tags",
      "accounts",
      "categories",
    ]);
  });
});
