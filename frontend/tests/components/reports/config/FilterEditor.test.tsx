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

  it("hides the Status control for a non-transactions (recurring) widget", async () => {
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

  it("hides the Amount range control for a non-transactions (recurring) widget", async () => {
    render({}, {}, () => {}, "recurring");
    await screen.findByTestId("category-picker");
    expect(screen.queryByTestId("amount-range-filter")).not.toBeInTheDocument();
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

  it("offers the Transfer transaction type for a transactions widget", async () => {
    render({}, {}, () => {}, "transactions");
    await screen.findByTestId("category-picker");
    expect(
      screen.getByLabelText("Widget transaction type Transfer"),
    ).toBeInTheDocument();
  });

  it("hides the Transfer transaction type for a recurring widget", async () => {
    render({}, {}, () => {}, "recurring");
    await screen.findByTestId("category-picker");
    expect(
      screen.getByLabelText("Widget transaction type Income"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Widget transaction type Expense"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Widget transaction type Transfer"),
    ).not.toBeInTheDocument();
  });

  it("self-heals a persisted txn_type=transfer on a recurring widget to 'Any'", async () => {
    const calls: WidgetFilters[] = [];
    // A widget persisted on transactions with txn_type=transfer, later
    // switched to recurring (where transfer is hidden). The orphan value
    // must self-correct on mount so the control never shows a phantom
    // no-selection-with-stale-value state.
    render({ txn_type: ["transfer"] }, {}, (next) => calls.push(next), "recurring");
    await screen.findByTestId("category-picker");
    // The self-heal effect MUST have fired (not vacuously empty) and cleared
    // the orphan value.
    expect(calls).toHaveLength(1);
    expect(calls.at(-1)?.txn_type).toBeUndefined();
  });

  it("does NOT self-heal a valid txn_type=transfer on a transactions widget", async () => {
    const calls: WidgetFilters[] = [];
    render({ txn_type: ["transfer"] }, {}, (next) => calls.push(next), "transactions");
    await screen.findByTestId("category-picker");
    // transfer is a valid choice for transactions → no auto-clear fires.
    expect(calls).toHaveLength(0);
  });

  it("self-heals by stripping only transfer and KEEPING the rest on a recurring widget", async () => {
    const calls: WidgetFilters[] = [];
    // A widget persisted on transactions with [expense, transfer], switched
    // to recurring (transfer hidden). The strip must drop ONLY transfer and
    // keep expense — NOT clear the whole filter to undefined.
    render(
      { txn_type: ["expense", "transfer"] },
      {},
      (next) => calls.push(next),
      "recurring",
    );
    await screen.findByTestId("category-picker");
    expect(calls).toHaveLength(1);
    expect(calls.at(-1)?.txn_type).toEqual(["expense"]);
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
