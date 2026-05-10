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
  { id: 202, name: "Entertainment", type: "expense", parent_id: 200, parent_name: "Lifestyle", description: null, slug: null, is_system: false, transaction_count: 0 },
  { id: 301, name: "Salary", type: "income", parent_id: 300, parent_name: "Income", description: null, slug: null, is_system: false, transaction_count: 1 },
  { id: 501, name: "Adjustments", type: "both", parent_id: 500, parent_name: "Mixed", description: null, slug: null, is_system: false, transaction_count: 0 },
];

describe("BatchMoveModal target type filter", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("expense-only selection excludes the source master (no-op rejected by backend) and BOTH/INCOME", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[101, 102]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    // Lifestyle (200) is the only valid expense target: same-type and not
    // a source master.
    expect(await screen.findByTestId("batch-move-target-200")).toBeInTheDocument();
    // Source master Food (100) is excluded: backend rejects
    // target_parent_id == sub.parent_id as a no-op.
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    // No BOTH masters and no INCOME masters.
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-600")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-300")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-400")).not.toBeInTheDocument();
  });

  it("income-only selection lists income masters except the source parent (not BOTH)", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[301]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    // Income Alt (400) is the only valid income target.
    expect(await screen.findByTestId("batch-move-target-400")).toBeInTheDocument();
    // Source master Income (300) is excluded.
    expect(screen.queryByTestId("batch-move-target-300")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-600")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-200")).not.toBeInTheDocument();
  });

  it("BOTH-only selection lists only BOTH masters as targets, excluding the source parent", async () => {
    render(
      <BatchMoveModal
        open
        selectedIds={[501]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    // Mixed Alt (600) is the only valid BOTH target.
    expect(await screen.findByTestId("batch-move-target-600")).toBeInTheDocument();
    // Source master Mixed (500) is excluded.
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-300")).not.toBeInTheDocument();
  });

  it("multi-source-master selection excludes ALL source masters from the picker", async () => {
    // 101 is under Food (100). 202 is under Lifestyle (200). Both expense.
    // Both source masters must be excluded; no valid expense target remains.
    render(
      <BatchMoveModal
        open
        selectedIds={[101, 202]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    // The picker should be empty because Food and Lifestyle are the only
    // expense masters in the fixture and both are sources.
    expect(
      await screen.findByTestId("batch-move-empty-message"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-200")).not.toBeInTheDocument();

    expect(screen.getByTestId("batch-move-confirm")).toBeDisabled();
  });

  it("when all compatible masters are sources, surfaces inline message and disables submit", async () => {
    // BOTH source 501 lives under Mixed (500). The fixture also has Mixed
    // Alt (600). To stage the all-compat-are-sources case, simulate the
    // user selecting subs from BOTH BOTH-masters: but the fixture only
    // has one BOTH sub. Build a one-off categories list for this test
    // where Mixed Alt also has a sub.
    const catsWithBothMasterSubs = [
      ...cats,
      {
        id: 601,
        name: "Reconciliations",
        type: "both" as const,
        parent_id: 600,
        parent_name: "Mixed Alt",
        description: null,
        slug: null,
        is_system: false,
        transaction_count: 0,
      },
    ];

    render(
      <BatchMoveModal
        open
        selectedIds={[501, 601]}
        categories={catsWithBothMasterSubs}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const message = await screen.findByTestId("batch-move-empty-message");
    expect(message.textContent).toMatch(/already parents/i);
    expect(screen.queryByTestId("batch-move-target-500")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-600")).not.toBeInTheDocument();

    expect(screen.getByTestId("batch-move-confirm")).toBeDisabled();
  });

  it("single source master selection: other compat masters appear, source master excluded", async () => {
    // 101 is under Food (100). The fixture has Lifestyle (200) as the
    // other expense master. Only 200 should appear.
    render(
      <BatchMoveModal
        open
        selectedIds={[101]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("batch-move-target-200")).toBeInTheDocument();
    expect(screen.queryByTestId("batch-move-target-100")).not.toBeInTheDocument();
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
