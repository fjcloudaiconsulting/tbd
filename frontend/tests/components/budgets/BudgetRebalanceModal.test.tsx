import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import BudgetRebalanceModal, {
  type RebalanceResponse,
} from "@/components/budgets/BudgetRebalanceModal";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const apiFetchMock = vi.mocked(apiFetch);

const budgets = [
  { id: 10, category_id: 1, amount: 400 },
  { id: 11, category_id: 2, amount: 200 },
];

const okResponse: RebalanceResponse = {
  status: "ok",
  period_start: "2026-05-01",
  summary: "Move money from dining to groceries.",
  suggestions: [
    {
      category_id: 1,
      category_name: "Groceries",
      current_amount: 400,
      suggested_amount: 450,
      delta_amount: 50,
      reasoning: "You consistently overspend on groceries.",
    },
    {
      category_id: 2,
      category_name: "Dining",
      current_amount: 200,
      suggested_amount: 150,
      delta_amount: -50,
      reasoning: "You under-spend here every month.",
    },
  ],
};

describe("BudgetRebalanceModal", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("does not render when open=false", () => {
    render(
      <BudgetRebalanceModal
        open={false}
        budgets={budgets}
        onApplied={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders the diff table on ok response", async () => {
    apiFetchMock.mockResolvedValueOnce(okResponse);
    render(
      <BudgetRebalanceModal
        open
        budgets={budgets}
        onApplied={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("rebalance-diff-table")).toBeInTheDocument(),
    );
    expect(screen.getByText("Groceries")).toBeInTheDocument();
    expect(screen.getByText("Dining")).toBeInTheDocument();
    // Suggestion only — nothing was applied. The endpoint that was
    // called must be the rebalance endpoint, never a PUT.
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/ai/budget/rebalance",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("renders empty state for llm_unavailable without crashing", async () => {
    apiFetchMock.mockResolvedValueOnce({
      status: "llm_unavailable",
      period_start: "2026-05-01",
      summary: "AI is unavailable.",
      suggestions: [],
    } satisfies RebalanceResponse);

    render(
      <BudgetRebalanceModal
        open
        budgets={budgets}
        onApplied={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("rebalance-empty-state")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("rebalance-diff-table")).not.toBeInTheDocument();
    // Apply button must not render in an empty state.
    expect(
      screen.queryByRole("button", { name: /Apply/i }),
    ).not.toBeInTheDocument();
  });

  it("applies only accepted rows via PUT /budgets/{id}", async () => {
    apiFetchMock
      .mockResolvedValueOnce(okResponse) // initial fetch
      .mockResolvedValue({}); // PUT writes
    const onApplied = vi.fn();
    const onClose = vi.fn();

    render(
      <BudgetRebalanceModal
        open
        budgets={budgets}
        onApplied={onApplied}
        onClose={onClose}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("rebalance-diff-table")).toBeInTheDocument(),
    );

    // Skip the Dining row (uncheck its checkbox).
    const diningCheckbox = screen.getByLabelText(
      /Apply suggestion for Dining/i,
    ) as HTMLInputElement;
    expect(diningCheckbox.checked).toBe(true);
    await act(async () => {
      fireEvent.click(diningCheckbox);
    });
    expect(diningCheckbox.checked).toBe(false);

    // Click Apply.
    const applyBtn = screen.getByRole("button", { name: /Apply 1 change/i });
    await act(async () => {
      fireEvent.click(applyBtn);
    });

    await waitFor(() => expect(onApplied).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();

    // First call is the rebalance fetch; subsequent calls are PUTs.
    // We accepted Groceries (category_id=1) which maps to budget id 10,
    // and skipped Dining. Therefore exactly one PUT must fire, to /10.
    const putCalls = apiFetchMock.mock.calls.filter((c) => {
      const init = c[1] as RequestInit | undefined;
      return init?.method === "PUT";
    });
    expect(putCalls).toHaveLength(1);
    expect(putCalls[0][0]).toBe("/api/v1/budgets/10");
    expect(putCalls[0][1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ amount: 450 }),
    });
  });

  it("does not call any PUT when the user clicks Cancel", async () => {
    apiFetchMock.mockResolvedValueOnce(okResponse);
    const onApplied = vi.fn();
    const onClose = vi.fn();

    render(
      <BudgetRebalanceModal
        open
        budgets={budgets}
        onApplied={onApplied}
        onClose={onClose}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("rebalance-diff-table")).toBeInTheDocument(),
    );

    const cancel = screen.getByRole("button", { name: /Cancel/i });
    await act(async () => {
      fireEvent.click(cancel);
    });

    expect(onClose).toHaveBeenCalled();
    expect(onApplied).not.toHaveBeenCalled();
    const putCalls = apiFetchMock.mock.calls.filter((c) => {
      const init = c[1] as RequestInit | undefined;
      return init?.method === "PUT";
    });
    expect(putCalls).toHaveLength(0);
  });
});
