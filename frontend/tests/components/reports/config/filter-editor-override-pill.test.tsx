/**
 * Regression: the "Overrides canvas" pill fires when the widget-level
 * filter actually DIFFERS from the canvas-level value (and never when
 * they match). Re-homed from override-pill-pickers onto the extracted
 * ``FilterEditor``, which owns the override pill and the picker filters.
 * The pickers feed the same ``WidgetFilters`` shape — these tests pin the
 * equality semantics survive on the extracted component.
 */
import { renderWithSWR, screen } from "../../../utils/render-with-swr";

import FilterEditor from "@/components/reports/config/FilterEditor";
import { apiFetch } from "@/lib/api";
import type { CanvasFilters, WidgetFilters } from "@/lib/reports/types";
import type { Category, Account } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
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
  {
    id: 2,
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
      return Promise.resolve([]) as Promise<unknown>;
    }
    return Promise.resolve([]);
  });
}

function renderEditor(
  filters: WidgetFilters,
  canvasFilters: CanvasFilters,
  onChange: (next: WidgetFilters) => void = () => {},
) {
  return renderWithSWR(
    <FilterEditor
      filters={filters}
      canvasFilters={canvasFilters}
      onChange={onChange}
    />,
  );
}

describe("Override pill — picker-based filters", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    mockApi();
  });

  it("does NOT show the pill when the widget category selection matches the canvas selection", async () => {
    renderEditor({ category_ids: [1, 2] }, { category_ids: [2, 1] });

    // Wait a tick for the SWR fetches to resolve so the picker has
    // populated the tree (pill renders synchronously off the prop
    // values).
    await screen.findByTestId("category-picker");
    // Pill is keyed by data-testid="override-pill"; assert none on
    // category_ids equality.
    const pills = screen.queryAllByTestId("override-pill");
    expect(pills.length).toBe(0);
  });

  it("DOES show the pill when the widget category selection differs from the canvas", async () => {
    renderEditor({ category_ids: [1] }, { category_ids: [2] });
    await screen.findByTestId("category-picker");
    const pills = screen.queryAllByTestId("override-pill");
    expect(pills.length).toBeGreaterThanOrEqual(1);
  });

  it("does NOT show the pill when the widget account selection matches the canvas selection", async () => {
    renderEditor({ account_ids: [1] }, { account_ids: [1] });
    await screen.findByTestId("account-filter");
    const pills = screen.queryAllByTestId("override-pill");
    expect(pills.length).toBe(0);
  });

  it("DOES show the pill when the widget account selection differs from the canvas", async () => {
    renderEditor({ account_ids: [2] }, { account_ids: [1] });
    await screen.findByTestId("account-filter");
    const pills = screen.queryAllByTestId("override-pill");
    expect(pills.length).toBeGreaterThanOrEqual(1);
  });

  it("keeps the pill off when widget account_ids is empty (inherit)", async () => {
    const onChange = vi.fn();
    const { rerender } = renderEditor({ account_ids: [1] }, { account_ids: [1] }, onChange);

    await screen.findByTestId("account-filter");
    // Empty widget account_ids means inherit — pill stays off.
    rerender(
      <FilterEditor
        filters={{ account_ids: [] }}
        canvasFilters={{ account_ids: [1] }}
        onChange={onChange}
      />,
    );
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });
});
