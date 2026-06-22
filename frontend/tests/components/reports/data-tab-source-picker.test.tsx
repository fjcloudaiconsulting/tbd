/**
 * DataTab source picker — the catalog-driven `Data source` select.
 *
 * The picker is now live: it lists every source from
 * `useReportSources()` and, on switch, resets any dimension that the
 * new source's catalog doesn't carry (otherwise the widget would 422
 * at query time against the backend `validate()`). These tests mock the
 * SWR hook to a two-source catalog and assert both behaviors.
 */
import { renderWithSWR, fireEvent, screen } from "../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import type {
  BarWidget,
  KPIWidget,
  LineWidget,
  SourceCatalogEntry,
  Widget,
} from "@/lib/reports/types";
import { useReportSources } from "@/lib/reports/use-report-sources";

vi.mock("@/lib/reports/use-report-sources", () => ({
  useReportSources: vi.fn(),
}));

const CATALOG: SourceCatalogEntry[] = [
  {
    key: "transactions",
    label: "Transactions",
    dimensions: [
      { key: "category", label: "Category", kind: "categorical" },
      { key: "account", label: "Account", kind: "categorical" },
      { key: "month", label: "Month", kind: "temporal" },
    ],
    measures: [
      { key: "sum_amount", label: "Sum of amount", agg: "sum", field: "amount", format: "currency" },
      { key: "avg_amount", label: "Average amount", agg: "avg", field: "amount", format: "currency" },
      { key: "count_rows", label: "Transaction count", agg: "count", field: "id", format: "number" },
    ],
    filters: [
      { field: "date", label: "Date", ops: ["between"], kind: "time" },
      { field: "amount", label: "Amount", ops: ["between"], kind: "amount" },
      { field: "category_id", label: "Category", ops: ["in"], kind: "category" },
      { field: "account_id", label: "Account", ops: ["in"], kind: "account" },
      { field: "txn_type", label: "Type", ops: ["eq"], kind: "type" },
      { field: "status", label: "Status", ops: ["eq"], kind: "status" },
      { field: "tag_name", label: "Tag", ops: ["in"], kind: "tag" },
    ],
  },
  {
    key: "accounts",
    label: "Accounts",
    dimensions: [
      { key: "account_type", label: "Account type", kind: "categorical" },
      { key: "currency", label: "Currency", kind: "categorical" },
    ],
    measures: [
      { key: "sum_balance", label: "Sum of balance", agg: "sum", field: "balance", format: "currency" },
      { key: "avg_balance", label: "Average balance", agg: "avg", field: "balance", format: "currency" },
      { key: "count_accounts", label: "Account count", agg: "count", field: "id", format: "number" },
    ],
    // Accounts publishes account_id but NOT category_id / txn_type /
    // amount / date / tag_name — a transactions widget's leftover
    // filters on those fields must be pruned on switch.
    filters: [
      { field: "account_id", label: "Account", ops: ["in"], kind: "account" },
      { field: "account_type", label: "Account type", ops: ["in"], kind: "account_type" },
      { field: "currency", label: "Currency", ops: ["in"], kind: "currency" },
      { field: "account_active", label: "Status", ops: ["eq"], kind: "boolean" },
      { field: "balance", label: "Balance", ops: ["between"], kind: "number" },
    ],
  },
  {
    key: "recurring",
    label: "Recurring",
    dimensions: [
      { key: "category", label: "Category", kind: "categorical" },
      { key: "account", label: "Account", kind: "categorical" },
      { key: "currency", label: "Currency", kind: "categorical" },
      { key: "txn_type", label: "Type", kind: "categorical" },
      { key: "frequency", label: "Frequency", kind: "categorical" },
      { key: "recurring_active", label: "Status", kind: "categorical" },
    ],
    measures: [
      { key: "sum_amount", label: "Sum of amount", agg: "sum", field: "amount", format: "currency" },
      { key: "avg_amount", label: "Average amount", agg: "avg", field: "amount", format: "currency" },
      { key: "count_recurring", label: "Recurring count", agg: "count", field: "id", format: "number" },
    ],
    // Recurring publishes account_id / category_id / currency / txn_type /
    // frequency / recurring_active / amount — but NOT date or tag_name.
    filters: [
      { field: "account_id", label: "Account", ops: ["in"], kind: "account" },
      { field: "category_id", label: "Category", ops: ["in"], kind: "category" },
      { field: "currency", label: "Currency", ops: ["in"], kind: "currency" },
      { field: "txn_type", label: "Type", ops: ["eq"], kind: "type" },
      { field: "frequency", label: "Frequency", ops: ["eq"], kind: "frequency" },
      { field: "recurring_active", label: "Status", ops: ["eq"], kind: "boolean" },
      { field: "amount", label: "Amount", ops: ["between"], kind: "amount" },
    ],
  },
];

beforeEach(() => {
  vi.mocked(useReportSources).mockReturnValue({
    sources: CATALOG,
    isLoading: false,
  });
});

function makeBar(): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

function makeKpi(): KPIWidget {
  return {
    id: "w_kpi",
    type: "kpi",
    title: "KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
    },
  };
}

/** A persisted accounts-source bar widget (the saved-and-reopened case). */
function makeAccountsBar(): BarWidget {
  return {
    id: "w_acct_bar",
    type: "bar",
    title: "Accounts bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "accounts",
      measure: { agg: "sum", field: "balance" },
      dimensions: ["account_type"],
    },
  };
}

/** A multi-series (line) transactions widget with two amount series. */
function makeLine(): LineWidget {
  return {
    id: "w_line",
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [
        { measure: { agg: "sum", field: "amount" } },
        { measure: { agg: "avg", field: "amount" } },
      ],
      dimensions: ["month"],
    },
  };
}

it("enables the Data source select and lists every catalog source", () => {
  renderWithSWR(<DataTab widget={makeBar()} onUpdate={vi.fn()} />);

  const select = screen.getByLabelText("Data source") as HTMLSelectElement;
  expect(select).not.toBeDisabled();
  expect(
    screen.getByRole("option", { name: "Accounts" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: "Transactions" }),
  ).toBeInTheDocument();
});

it("switches dataset and drops a now-invalid dimension on source change", () => {
  const onUpdate = vi.fn();
  renderWithSWR(<DataTab widget={makeBar()} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "accounts" },
  });

  expect(onUpdate).toHaveBeenCalledTimes(1);
  const next = onUpdate.mock.calls[0][0] as Widget;
  expect(next.config.dataset).toBe("accounts");
  // "category" is not in the accounts catalog — it must be gone, replaced
  // by the accounts source's first dimension key.
  const dims = (next.config as BarWidget["config"]).dimensions;
  expect(dims).not.toContain("category");
  expect(dims[0]).toBe("account_type");
});

it("narrows measure FIELD options to the accounts source (balance, not amount)", () => {
  // Render a widget already on the accounts source so the field select is
  // driven by the accounts catalog.
  renderWithSWR(<DataTab widget={makeAccountsBar()} onUpdate={vi.fn()} />);

  const fieldSelect = screen.getByLabelText("Field") as HTMLSelectElement;
  const optionValues = Array.from(fieldSelect.options).map((o) => o.value);
  expect(optionValues).toContain("balance");
  expect(optionValues).not.toContain("amount");
  // The distinct fields the accounts source publishes: balance + id.
  expect(optionValues).toEqual(["balance", "id"]);
});

it("resets the measure to a valid accounts measure on switch to accounts", () => {
  const onUpdate = vi.fn();
  renderWithSWR(<DataTab widget={makeBar()} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "accounts" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  // The measure must NOT be left on the transactions field ``amount`` —
  // that 422s at query time. It resets to the accounts source's first
  // measure (sum_balance).
  expect(next.config.measure.field).not.toBe("amount");
  expect(next.config.measure).toEqual({ agg: "sum", field: "balance" });
});

it("KPI widget: switching source doesn't crash and resets its measure", () => {
  const onUpdate = vi.fn();
  renderWithSWR(<DataTab widget={makeKpi()} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "accounts" },
  });

  expect(onUpdate).toHaveBeenCalledTimes(1);
  const next = onUpdate.mock.calls[0][0] as KPIWidget;
  expect(next.config.dataset).toBe("accounts");
  expect(next.config.measure).toEqual({ agg: "sum", field: "balance" });
});

it("switch back accounts→transactions resets dims + measure to valid defaults", () => {
  const onUpdate = vi.fn();
  renderWithSWR(<DataTab widget={makeAccountsBar()} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "transactions" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  expect(next.config.dataset).toBe("transactions");
  // ``account_type`` isn't a transactions dimension → dropped, refilled
  // with the transactions source's first dimension.
  expect(next.config.dimensions).not.toContain("account_type");
  expect(next.config.dimensions[0]).toBe("category");
  // ``balance`` isn't a transactions field → measure reset to sum_amount.
  expect(next.config.measure).toEqual({ agg: "sum", field: "amount" });
});

it("multi-series widget: switching to accounts collapses measures to one valid accounts series", () => {
  const onUpdate = vi.fn();
  renderWithSWR(<DataTab widget={makeLine()} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "accounts" },
  });

  expect(onUpdate).toHaveBeenCalledTimes(1);
  const next = onUpdate.mock.calls[0][0] as LineWidget;
  expect(next.config.dataset).toBe("accounts");
  // Both source series referenced ``amount`` (a transactions-only field)
  // → collapse to a single valid accounts series (sum_balance).
  expect(next.config.measures).toHaveLength(1);
  const accountsFields = new Set(["balance", "id"]);
  for (const s of next.config.measures) {
    expect(accountsFields.has(s.measure.field)).toBe(true);
  }
  expect(next.config.measures[0].measure).toEqual({
    agg: "sum",
    field: "balance",
  });
  // ``month`` isn't an accounts dimension → dropped, refilled with the
  // accounts source's first dimension key.
  expect(next.config.dimensions).not.toContain("month");
  expect(next.config.dimensions[0]).toBe("account_type");
});

it("prunes stale per-widget filters the new source doesn't publish on switch", () => {
  const onUpdate = vi.fn();
  const widget: BarWidget = {
    ...makeBar(),
    config: {
      ...makeBar().config,
      filters: {
        // Survives: accounts publishes ``account_id``.
        account_ids: [1, 2],
        // All pruned: accounts publishes none of these fields.
        category_ids: [9],
        txn_type: ["expense"],
        amount_range: { min: 10 },
        tag_names: ["foo"],
        tag_match: "any",
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      },
    },
  };
  renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "accounts" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  expect(next.config.dataset).toBe("accounts");
  // Only ``account_ids`` (→ account_id, published by accounts) survives.
  expect(next.config.filters).toEqual({ account_ids: [1, 2] });
});

it("drops the whole filters blob when no widget filter survives the switch", () => {
  const onUpdate = vi.fn();
  const widget: BarWidget = {
    ...makeBar(),
    config: {
      ...makeBar().config,
      filters: { category_ids: [9], txn_type: ["expense"] },
    },
  };
  renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "accounts" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  // category_ids + txn_type both unpublished by accounts → no filters.
  expect(next.config.filters).toBeUndefined();
});

it("lists the Recurring source and switches dataset to recurring with valid defaults", () => {
  const onUpdate = vi.fn();
  renderWithSWR(<DataTab widget={makeBar()} onUpdate={onUpdate} />);

  expect(
    screen.getByRole("option", { name: "Recurring" }),
  ).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "recurring" },
  });

  expect(onUpdate).toHaveBeenCalledTimes(1);
  const next = onUpdate.mock.calls[0][0] as BarWidget;
  expect(next.config.dataset).toBe("recurring");
  // ``category`` is a recurring dimension → kept as the primary dim.
  expect(next.config.dimensions[0]).toBe("category");
  // sum_amount → field ``amount`` is a valid recurring measure, so the
  // transactions measure ({sum, amount}) survives the switch unchanged.
  expect(next.config.measure).toEqual({ agg: "sum", field: "amount" });
});

it("offers the recurring-only Frequency dimension after switching to recurring", () => {
  const onUpdate = vi.fn();
  // Render a widget already on the recurring source so the dimension select
  // is driven by the recurring catalog.
  const recurringBar: BarWidget = {
    ...makeBar(),
    config: {
      dataset: "recurring",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
  renderWithSWR(<DataTab widget={recurringBar} onUpdate={onUpdate} />);

  const dimSelect = screen.getByLabelText(
    "Primary dimension",
  ) as HTMLSelectElement;
  const labels = Array.from(dimSelect.options).map((o) => o.textContent);
  expect(labels).toContain("Frequency");
  expect(labels).toContain("Status");
});

it("prunes tag_names + date_range but keeps category_ids on switch to recurring", () => {
  const onUpdate = vi.fn();
  const widget: BarWidget = {
    ...makeBar(),
    config: {
      ...makeBar().config,
      filters: {
        // Survives: recurring publishes ``category_id`` and ``account_id``.
        category_ids: [9],
        account_ids: [1, 2],
        // Pruned: recurring publishes neither ``tag_name`` nor ``date``.
        tag_names: ["foo"],
        tag_match: "any",
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      },
    },
  };
  renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "recurring" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  expect(next.config.dataset).toBe("recurring");
  // category_ids + account_ids survive; tag_names/tag_match/date_range gone.
  expect(next.config.filters).toEqual({
    category_ids: [9],
    account_ids: [1, 2],
  });
});

it("clears a stale txn_type=transfer on switch to recurring (transfer invalid there)", () => {
  const onUpdate = vi.fn();
  const widget: BarWidget = {
    ...makeBar(),
    config: {
      ...makeBar().config,
      filters: {
        // recurring publishes ``txn_type`` so the FIELD survives the prune,
        // but ``transfer`` is invalid for recurring (income/expense only) —
        // the stale VALUE must be stripped or the backend validate() 422s.
        txn_type: ["transfer"],
      },
    },
  };
  renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "recurring" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  expect(next.config.dataset).toBe("recurring");
  // ``txn_type`` was the only filter and its value was invalid → whole
  // blob drops to undefined (matching pruneFiltersToSource's empty behavior).
  expect(next.config.filters).toBeUndefined();
});

it("keeps a valid txn_type but clears transfer on switch to recurring", () => {
  const onUpdate = vi.fn();
  const widget: BarWidget = {
    ...makeBar(),
    config: {
      ...makeBar().config,
      filters: {
        txn_type: ["transfer"],
        // survives: recurring publishes ``category_id``.
        category_ids: [9],
      },
    },
  };
  renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "recurring" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  // category_ids survives; the invalid transfer txn_type is stripped.
  expect(next.config.filters).toEqual({ category_ids: [9] });
});

it("keeps a valid txn_type=expense on switch to recurring", () => {
  const onUpdate = vi.fn();
  const widget: BarWidget = {
    ...makeBar(),
    config: {
      ...makeBar().config,
      filters: { txn_type: ["expense"] },
    },
  };
  renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);

  fireEvent.change(screen.getByLabelText("Data source"), {
    target: { value: "recurring" },
  });

  const next = onUpdate.mock.calls[0][0] as BarWidget;
  // ``expense`` is valid for recurring → preserved unchanged.
  expect(next.config.filters).toEqual({ txn_type: ["expense"] });
});

it("persisted accounts widget rendered before /sources resolves has no value/options mismatch", () => {
  // Catalog still loading: sources empty. The accounts widget's stored
  // dimension (``account_type``) and field (``balance``) must still have a
  // matching <option> so the rendered selects' values are valid options.
  vi.mocked(useReportSources).mockReturnValue({
    sources: [],
    isLoading: true,
  });

  renderWithSWR(<DataTab widget={makeAccountsBar()} onUpdate={vi.fn()} />);

  // Assert positively that every controlled select's value is one of its
  // own option values — a real "value not in options" mismatch would
  // leave the select with an empty/absent value here.
  const dimSelect = screen.getByLabelText("Primary dimension") as HTMLSelectElement;
  expect(dimSelect.value).toBe("account_type");
  expect(
    Array.from(dimSelect.options).map((o) => o.value),
  ).toContain(dimSelect.value);

  const fieldSelect = screen.getByLabelText("Field") as HTMLSelectElement;
  expect(fieldSelect.value).toBe("balance");
  expect(
    Array.from(fieldSelect.options).map((o) => o.value),
  ).toContain(fieldSelect.value);
});
