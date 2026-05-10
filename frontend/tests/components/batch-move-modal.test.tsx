import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import BatchMoveModal from "@/components/categories/BatchMoveModal";
import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const cats: Category[] = [
  // Masters
  { id: 100, name: "Food", type: "expense", parent_id: null, parent_name: null, description: null, slug: "food_dining", is_system: true, transaction_count: 0 },
  { id: 200, name: "Lifestyle", type: "expense", parent_id: null, parent_name: null, description: null, slug: "lifestyle", is_system: true, transaction_count: 0 },
  { id: 300, name: "Income", type: "income", parent_id: null, parent_name: null, description: null, slug: "income", is_system: true, transaction_count: 0 },
  { id: 400, name: "Income Alt", type: "income", parent_id: null, parent_name: null, description: null, slug: null, is_system: false, transaction_count: 0 },
  { id: 500, name: "Mixed", type: "both", parent_id: null, parent_name: null, description: null, slug: null, is_system: false, transaction_count: 0 },
  { id: 600, name: "Mixed Alt", type: "both", parent_id: null, parent_name: null, description: null, slug: null, is_system: false, transaction_count: 0 },
  // Subs
  { id: 101, name: "Restaurants", type: "expense", parent_id: 100, parent_name: "Food", description: null, slug: null, is_system: false, transaction_count: 5 },
  { id: 102, name: "Groceries", type: "expense", parent_id: 100, parent_name: "Food", description: null, slug: null, is_system: false, transaction_count: 0 },
  { id: 301, name: "Salary", type: "income", parent_id: 300, parent_name: "Income", description: null, slug: null, is_system: false, transaction_count: 1 },
  { id: 501, name: "Adjustments", type: "both", parent_id: 500, parent_name: "Mixed", description: null, slug: null, is_system: false, transaction_count: 0 },
];

describe("BatchMoveModal target type filter", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("expense-only selection lists only expense masters as targets (not BOTH)", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[101, 102]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("batch-move-target-200")).toBeInTheDocument();
    // Same-type expense masters: Food (100) is the source-master but is
    // still a candidate UI-wise because the picker simply hides BOTH and
    // INCOME masters. The backend will reject same-master moves; the UI
    // does not pre-filter by source master id.
    expect(screen.getByTestId("batch-move-target-100")).toBeInTheDocument();
    // No BOTH masters and no INCOME masters.
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-600")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-300")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-400")).not.toBeInTheDocument();
  });

  it("income-only selection lists only income masters as targets (not BOTH)", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[301]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("batch-move-target-300")).toBeInTheDocument();
    expect(screen.getByTestId("batch-move-target-400")).toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-600")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-200")).not.toBeInTheDocument();
  });

  it("BOTH-only selection lists only BOTH masters as targets", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[501]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("batch-move-target-500")).toBeInTheDocument();
    expect(screen.getByTestId("batch-move-target-600")).toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-300")).not.toBeInTheDocument();
  });

  it("mixed-type selection shows no targets and an inline warning, submit is disabled", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[101, 301]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("batch-move-mixed-warning")).toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-200")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-300")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();

    const confirm = screen.getByTestId("batch-move-confirm");
    expect(confirm).toBeDisabled();
  });
});

describe("BatchMoveModal async onSuccess", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("awaits async onSuccess and surfaces refresh errors with a Retry button", async () => {
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (url.includes("/move/preview")) {
        return Promise.resolve({
          category_id: 101,
          source_master_id: 100,
          target_master_id: 200,
          affected_transaction_count: 5,
          affected_recurring_count: 0,
          affected_forecast_item_count: 0,
          budget_actuals_shifted: false,
        });
      }
      if (url === "/api/v1/categories/batch-move" && init?.method === "POST") {
        return Promise.resolve({ moves: [] });
      }
      return Promise.resolve({});
    }) as never);

    let invocations = 0;
    const onSuccess = vi.fn(async () => {
      invocations += 1;
      if (invocations === 1) throw new Error("network blip");
    });

    render(
      <BatchMoveModal
        open
        selectedIds={[101]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={onSuccess}
      />,
    );

    fireEvent.click(await screen.findByTestId("batch-move-target-200"));
    await waitFor(() => {
      expect(screen.getByTestId("batch-move-preview")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("batch-move-confirm"));

    const banner = await screen.findByTestId("batch-move-refresh-error");
    expect(banner.textContent).toMatch(/network blip/);

    fireEvent.click(screen.getByTestId("batch-move-refresh-retry"));
    await waitFor(() => {
      expect(screen.queryByTestId("batch-move-refresh-error")).not.toBeInTheDocument();
    });
    expect(invocations).toBe(2);
  });
});
