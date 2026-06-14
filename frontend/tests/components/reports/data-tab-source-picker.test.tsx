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
    filters: [],
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
    filters: [],
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

it("persisted accounts widget rendered before /sources resolves has no value/options mismatch", () => {
  // Catalog still loading: sources empty. The accounts widget's stored
  // dimension (``account_type``) and field (``balance``) must still have a
  // matching <option> so no React "value not in options" warning fires.
  vi.mocked(useReportSources).mockReturnValue({
    sources: [],
    isLoading: true,
  });
  const warn = vi.spyOn(console, "error").mockImplementation(() => {});

  renderWithSWR(<DataTab widget={makeAccountsBar()} onUpdate={vi.fn()} />);

  const dimSelect = screen.getByLabelText("Primary dimension") as HTMLSelectElement;
  expect(dimSelect.value).toBe("account_type");
  expect(
    Array.from(dimSelect.options).some((o) => o.value === "account_type"),
  ).toBe(true);
  // No controlled-select value/options mismatch warning.
  expect(warn).not.toHaveBeenCalledWith(
    expect.stringContaining("value"),
    expect.anything(),
    expect.anything(),
  );
  warn.mockRestore();
});
