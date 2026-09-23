/**
 * Per-widget FilterEditor extracted from the original config rail. Pins the six filter
 * fields render, the override-pill parity (driven by resolve.ts'
 * isFieldOverridden, not reimplemented), and the amount-range merge.
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import FilterEditor from "@/components/reports/config/FilterEditor";
import { apiFetch } from "@/lib/api";
import type { CanvasFilters, Dataset, WidgetFilters } from "@/lib/reports/types";
import type { Account, Category } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));
// TBD-381: control visibility is catalog-driven. Without a catalog these
// tests hit the deliberate "unknown source -> allow everything" bias and see
// every control, so they must supply one to assert real published sets.
vi.mock("@/lib/reports/use-report-sources", async () => {
  const { ALL_ENTRIES } = await import("../../../utils/mock-report-sources");
  return { useReportSources: () => ({ sources: ALL_ENTRIES, isLoading: false }) };
});
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ user: { id: 1 }, loading: false }),
}));

const CATEGORIES: Category[] = [
  {
    id: 1,
    name: "Food",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "food",
    is_system: false,
    transaction_count: 0,
  },
];

const ACCOUNTS: Account[] = [
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
];

function mockApi() {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockImplementation((path) => {
    if (String(path).startsWith("/api/v1/categories")) {
      return Promise.resolve(CATEGORIES) as Promise<unknown>;
    }
    if (String(path).startsWith("/api/v1/accounts")) {
      return Promise.resolve(ACCOUNTS) as Promise<unknown>;
    }
    if (String(path).startsWith("/api/v1/tags")) {
      return Promise.resolve([
        { id: 1, name: "groceries", name_normalized: "groceries", usage_count: 3 },
      ]) as Promise<unknown>;
    }
    return Promise.resolve([]);
  });
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  mockApi();
});

function render(
  filters: WidgetFilters,
  canvasFilters: CanvasFilters,
  onChange: (next: WidgetFilters) => void = () => {},
  dataset: Dataset = "transactions",
) {
  return renderWithSWR(
    <FilterEditor
      filters={filters}
      canvasFilters={canvasFilters}
      onChange={onChange}
      dataset={dataset}
    />,
  );
}

describe("FilterEditor", () => {
  it("renders all six filter fields", async () => {
    render({}, {});
    await screen.findByTestId("category-picker");
    expect(screen.getByText("Date range")).toBeInTheDocument();
    expect(screen.getByText("Accounts")).toBeInTheDocument();
    expect(screen.getByText("Categories")).toBeInTheDocument();
    expect(screen.getByText("Transaction type")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount min")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount max")).toBeInTheDocument();
    expect(screen.getByTestId("account-filter")).toBeInTheDocument();
  });

  it("shows the override pill when the widget date range differs from canvas", async () => {
    render(
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
      { date_range: { start: "2026-02-01", end: "2026-02-28" } },
    );
    await screen.findByTestId("category-picker");
    expect(screen.getAllByTestId("override-pill").length).toBeGreaterThanOrEqual(1);
  });

  it("does not show the override pill when the date range matches canvas", async () => {
    render(
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
    );
    await screen.findByTestId("category-picker");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("shows the override pill when the widget status differs from canvas", async () => {
    // Dates unset on both sides → the only override pill is the status one.
    render({ status: "settled" }, { status: "pending" });
    await screen.findByTestId("category-picker");
    expect(screen.getAllByTestId("override-pill").length).toBe(1);
  });

  it("does not show the status override pill when status matches canvas", async () => {
    render({ status: "settled" }, { status: "settled" });
    await screen.findByTestId("category-picker");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("merges amount min into amount_range on change", async () => {
    const calls: WidgetFilters[] = [];
    render({}, {}, (next) => calls.push(next));
    await screen.findByTestId("category-picker");
    fireEvent.change(screen.getByLabelText("Widget amount min"), {
      target: { value: "5" },
    });
    expect(calls.at(-1)?.amount_range).toEqual({ min: 5 });
  });

  it("merges amount max while preserving an existing min", async () => {
    const calls: WidgetFilters[] = [];
    render({ amount_range: { min: 5 } }, {}, (next) => calls.push(next));
    await screen.findByTestId("category-picker");
    fireEvent.change(screen.getByLabelText("Widget amount max"), {
      target: { value: "20" },
    });
    expect(calls.at(-1)?.amount_range).toEqual({ min: 5, max: 20 });
  });

  it("checks a type into a single-element array", async () => {
    const calls: WidgetFilters[] = [];
    render({}, {}, (next) => calls.push(next));
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Widget transaction type Expense"));
    expect(calls.at(-1)?.txn_type).toEqual(["expense"]);
  });

  it("accumulates multiple checked types into the array", async () => {
    const calls: WidgetFilters[] = [];
    render({ txn_type: ["income"] }, {}, (next) => calls.push(next));
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Widget transaction type Expense"));
    expect(calls.at(-1)?.txn_type).toEqual(["income", "expense"]);
  });

  it("clears txn_type to undefined when the last checked type is unchecked", async () => {
    const calls: WidgetFilters[] = [];
    render({ txn_type: ["expense"] }, {}, (next) => calls.push(next));
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Widget transaction type Expense"));
    expect(calls.at(-1)?.txn_type).toBeUndefined();
  });

  it("offers the Status control (All/Settled/Pending) for a transactions widget", async () => {
    render({}, {}, () => {}, "transactions");
    await screen.findByTestId("category-picker");
    expect(screen.getByTestId("status-filter")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget status All")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget status Settled")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget status Pending")).toBeInTheDocument();
  });

  it("hides the Status control for a recurring widget, which does not publish it", async () => {
    // Unchanged outcome, different REASON: gated on the catalog now, not on
    // `dataset === "transactions"`.
    render({}, {}, () => {}, "recurring");
    await screen.findByTestId("category-picker");
    expect(screen.queryByTestId("status-filter")).not.toBeInTheDocument();
  });

  it("offers the Amount range control for a transactions widget", async () => {
    render({}, {}, () => {}, "transactions");
    await screen.findByTestId("category-picker");
    expect(screen.getByTestId("amount-range-filter")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount min")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount max")).toBeInTheDocument();
  });

  it("OFFERS the Amount range control for recurring, which publishes it (TBD-381)", async () => {
    // ⚠ This expectation was INVERTED, and it encoded the bug. `recurring`
    // publishes an `amount` filter (recurring.py), but the editor gated the
    // control on `dataset === "transactions"` and hid it -- the inverse of the
    // owner-reported symptom: a control withheld for a source that supports it.
    render({}, {}, () => {}, "recurring");
    await screen.findByTestId("category-picker");
    expect(screen.getByTestId("amount-range-filter")).toBeInTheDocument();
  });

  // TBD-471 RULING 1: the old "Include transfers & adjustments" checkbox is
  // replaced by a single 3-state radio axis (Exclude / Include / Only).
  it("offers the 3-state Transfers radio group for a transactions widget", async () => {
    render({}, {}, () => {}, "transactions");
    await screen.findByTestId("category-picker");
    expect(screen.getByText("Transfers")).toBeInTheDocument();
    expect(screen.getByLabelText("Exclude transfers (default)")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Include transfers & adjustments"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Only transfers")).toBeInTheDocument();
  });

  it("hides the Transfers radio group for a non-transactions widget — catalog-gated, not dataset-gated", async () => {
    render({}, {}, () => {}, "recurring");
    await screen.findByTestId("category-picker");
    expect(screen.queryByText("Transfers")).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Exclude transfers (default)"),
    ).not.toBeInTheDocument();
  });

  it("defaults to 'Exclude transfers (default)' checked when neither key is set", async () => {
    render({}, {}, () => {}, "transactions");
    await screen.findByTestId("category-picker");
    expect(screen.getByLabelText("Exclude transfers (default)")).toBeChecked();
  });

  // fence transfer_mode_three_states_READ
  it("reads state 'only' when both keys are set — transfers_only wins over include_non_reportable", async () => {
    render(
      { include_non_reportable: true, transfers_only: true },
      {},
      () => {},
      "transactions",
    );
    await screen.findByTestId("category-picker");
    expect(screen.getByLabelText("Only transfers")).toBeChecked();
    expect(
      screen.getByLabelText("Include transfers & adjustments"),
    ).not.toBeChecked();
  });

  // fence transfer_mode_three_states_WRITE — the bug lives here, not in the
  // read direction: a handler that writes its own key and leaves the other
  // set would strand the chart on "only" while the control reads "exclude".
  it("selecting 'Exclude transfers' clears BOTH include_non_reportable and transfers_only", async () => {
    const calls: WidgetFilters[] = [];
    render(
      { include_non_reportable: true, transfers_only: true },
      {},
      (next) => calls.push(next),
      "transactions",
    );
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Exclude transfers (default)"));
    expect(calls.at(-1)?.include_non_reportable).toBeUndefined();
    expect(calls.at(-1)?.transfers_only).toBeUndefined();
  });

  it("selecting 'Include transfers & adjustments' sets include_non_reportable and clears transfers_only", async () => {
    const calls: WidgetFilters[] = [];
    render({ transfers_only: true }, {}, (next) => calls.push(next), "transactions");
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Include transfers & adjustments"));
    expect(calls.at(-1)?.include_non_reportable).toBe(true);
    expect(calls.at(-1)?.transfers_only).toBeUndefined();
  });

  it("selecting 'Only transfers' sets transfers_only and clears include_non_reportable", async () => {
    const calls: WidgetFilters[] = [];
    render({ include_non_reportable: true }, {}, (next) => calls.push(next), "transactions");
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Only transfers"));
    expect(calls.at(-1)?.transfers_only).toBe(true);
    expect(calls.at(-1)?.include_non_reportable).toBeUndefined();
  });

  // guard sankey_panel_hides_both_controls — assert CHILD COUNT (not "no
  // gap", which is unassertable in jsdom): hideTypeControls removes both the
  // txn_type row and the transfers group as actual DOM nodes.
  it("guard: hideTypeControls removes both the type row and the transfers group as DOM nodes", async () => {
    const { rerender } = render({}, {}, () => {}, "transactions");
    await screen.findByTestId("category-picker");
    expect(screen.getByText("Transaction type")).toBeInTheDocument();
    expect(screen.getByText("Transfers")).toBeInTheDocument();
    const withControls = screen.getByTestId("filter-editor-root").children.length;

    rerender(
      <FilterEditor
        filters={{}}
        canvasFilters={{}}
        dataset="transactions"
        hideTypeControls
        onChange={() => {}}
      />,
    );
    await screen.findByTestId("category-picker");
    expect(screen.queryByText("Transaction type")).not.toBeInTheDocument();
    expect(screen.queryByText("Transfers")).not.toBeInTheDocument();
    const withoutControls = screen.getByTestId("filter-editor-root").children.length;
    expect(withoutControls).toBe(withControls - 2);
  });

  it("sets status on change", async () => {
    const calls: WidgetFilters[] = [];
    render({}, {}, (next) => calls.push(next), "transactions");
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Widget status Pending"));
    expect(calls.at(-1)?.status).toBe("pending");
  });

  it("clears status back to undefined when 'All' is chosen", async () => {
    const calls: WidgetFilters[] = [];
    render({ status: "pending" }, {}, (next) => calls.push(next), "transactions");
    await screen.findByTestId("category-picker");
    fireEvent.click(screen.getByLabelText("Widget status All"));
    expect(calls.at(-1)?.status).toBeUndefined();
  });

  // A persisted ``txn_type`` blob that predates TBD-471 could still hold the
  // string "transfer" on disk even though ``TxnType`` no longer admits it —
  // hence the cast, only ever used to model that legacy JSON shape.
  function withStaleTransfer(values: string[]): WidgetFilters {
    return { txn_type: values } as unknown as WidgetFilters;
  }

  // guard no_transfer_choice_on_any_dataset (TBD-471 RULING 2).
  // ⚠ INVERTED from the pre-TBD-471 version of this test (which asserted
  // transactions DOES offer Transfer, and recurring self-heals a stale
  // value). "Transfer" retired from ``TxnType`` entirely — it is no longer
  // offered on ANY dataset, transactions included, and there is no more
  // self-heal effect: ``asTxnTypeArray`` drops a stale persisted value
  // silently at read time, on every dataset, so ``onChange`` never fires
  // for it.
  it("never offers Transfer as a transaction-type choice, on any dataset, even with a persisted value", async () => {
    const txnCalls: WidgetFilters[] = [];
    render(withStaleTransfer(["transfer"]), {}, (next) => txnCalls.push(next), "transactions");
    await screen.findByTestId("category-picker");
    expect(screen.getByLabelText("Widget transaction type Income")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget transaction type Expense")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Widget transaction type Transfer"),
    ).not.toBeInTheDocument();
    expect(txnCalls).toHaveLength(0);
  });

  it("silently drops a persisted stale 'transfer' value, keeping the rest, with no onChange call", async () => {
    const calls: WidgetFilters[] = [];
    render(
      withStaleTransfer(["expense", "transfer"]),
      {},
      (next) => calls.push(next),
      "recurring",
    );
    await screen.findByTestId("category-picker");
    expect(
      screen.queryByLabelText("Widget transaction type Transfer"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Widget transaction type Expense")).toBeChecked();
    // No self-heal effect anymore — asTxnTypeArray already filtered the
    // stale value out of what's rendered, so there is nothing to write back.
    expect(calls).toHaveLength(0);
  });

  it("reports tag_names + tag_match when a tag chip is selected", async () => {
    const calls: WidgetFilters[] = [];
    render({}, {}, (next) => calls.push(next));
    const chip = await screen.findByTestId("tag-filter-chip-groceries");
    fireEvent.click(chip);
    expect(calls.at(-1)?.tag_names).toEqual(["groceries"]);
    expect(calls.at(-1)?.tag_match).toBe("all");
  });
});
