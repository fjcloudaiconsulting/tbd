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
import type { BarWidget, SourceCatalogEntry, Widget } from "@/lib/reports/types";
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
