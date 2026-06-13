/**
 * Phase 4b: ``date_range`` is the ONLY canvas-shared field, so it's the
 * only field whose widget value can "override" the canvas and show the
 * "Overrides canvas" pill. Accounts and categories are widget-only now
 * (the canvas no longer carries them), so ``isFieldOverridden`` always
 * returns false for them and their override pills never render — pinned
 * below. The surviving override case is date_range.
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

describe("Override pill — phase 4b (date-only canvas)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    mockApi();
  });

  it("NEVER shows the pill for category — categories are widget-only (match)", async () => {
    renderEditor({ category_ids: [1, 2] }, {});
    await screen.findByTestId("category-picker");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("NEVER shows the pill for category — categories are widget-only (set)", async () => {
    renderEditor({ category_ids: [1] }, {});
    await screen.findByTestId("category-picker");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("NEVER shows the pill for account — accounts are widget-only (match)", async () => {
    renderEditor({ account_ids: [1] }, {});
    await screen.findByTestId("account-filter");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("NEVER shows the pill for account — accounts are widget-only (set)", async () => {
    renderEditor({ account_ids: [1] }, {});
    await screen.findByTestId("account-filter");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("does NOT show the pill when the widget date matches the canvas date", async () => {
    renderEditor(
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
    );
    await screen.findByTestId("account-filter");
    expect(screen.queryAllByTestId("override-pill").length).toBe(0);
  });

  it("DOES show the pill when the widget date differs from the canvas date", async () => {
    renderEditor(
      { date_range: { start: "2026-02-01", end: "2026-02-28" } },
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
    );
    await screen.findByTestId("account-filter");
    expect(screen.queryAllByTestId("override-pill").length).toBeGreaterThanOrEqual(1);
  });
});
