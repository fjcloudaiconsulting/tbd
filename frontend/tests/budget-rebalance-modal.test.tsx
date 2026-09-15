// BudgetRebalanceModal — zero-sum balance meter + uncovered banner.
//
// The rebalance is a fixed-total reallocation: accepting the full set of
// suggestions nets to zero. The meter reads "balanced" by default and
// turns amber the moment the accepted selection drifts off zero-sum.
// An honest "uncovered overspend" banner shows when spending exceeds the
// total budget, and an empty state covers the no-surplus refusal.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import BudgetRebalanceModal from "@/components/budgets/BudgetRebalanceModal";
import { apiFetch } from "@/lib/api";
import { setBalancesHidden } from "@/lib/format";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const BUDGETS = [
  { id: 11, category_id: 1, amount: 100 },
  { id: 12, category_id: 2, amount: 90 },
];

const OK = {
  status: "ok",
  period_start: "2026-06-01",
  total_budget: 190,
  total_suggested: 190,
  uncovered_overspend: 0,
  is_balanced: true,
  summary: "Shift to bills",
  suggestions: [
    {
      category_id: 1,
      category_name: "Transportation",
      current_amount: 100,
      suggested_amount: 90,
      delta_amount: -10,
      reasoning: "free surplus",
    },
    {
      category_id: 2,
      category_name: "Bills",
      current_amount: 90,
      suggested_amount: 100,
      delta_amount: 10,
      reasoning: "cover rent",
    },
  ],
};

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
});

it("shows a balanced meter and warns when the selection drifts", async () => {
  vi.mocked(apiFetch).mockResolvedValue(OK as never);
  render(
    <BudgetRebalanceModal
      open
      budgets={BUDGETS}
      onApplied={() => {}}
      onClose={() => {}}
    />,
  );

  // Balanced by default: the full accepted set nets to zero.
  const meter = await screen.findByTestId("rebalance-balance-meter");
  expect(meter).toHaveTextContent(/balanced/i);

  // Unchecking the -10 row breaks zero-sum → amber warning.
  fireEvent.click(screen.getByLabelText("Apply suggestion for Transportation"));
  expect(screen.getByTestId("rebalance-balance-meter")).toHaveTextContent(
    /changes your total budget/i,
  );
});

it("renders an uncovered-overspend banner when spending exceeds budget", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    ...OK,
    uncovered_overspend: 30,
    is_balanced: false,
  } as never);
  render(
    <BudgetRebalanceModal
      open
      budgets={BUDGETS}
      onApplied={() => {}}
      onClose={() => {}}
    />,
  );

  const banner = await screen.findByTestId("rebalance-uncovered");
  expect(banner).toHaveTextContent(/over plan/i);
});

it("renders a friendly empty state when there is no surplus to move", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "empty_no_surplus",
    period_start: "2026-06-01",
    suggestions: [],
    summary: "Every category is projected at or over budget.",
    total_budget: 190,
    total_suggested: 190,
    uncovered_overspend: 0,
    is_balanced: true,
  } as never);
  render(
    <BudgetRebalanceModal
      open
      budgets={BUDGETS}
      onApplied={() => {}}
      onClose={() => {}}
    />,
  );

  await waitFor(() =>
    expect(screen.getByTestId("rebalance-empty-state")).toHaveTextContent(
      /nothing to reallocate/i,
    ),
  );
});

it("Hide balances masks the server-built reasoning and summary (TBD-527)", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    ...OK,
    summary: "Move 7373.37 from surplus to bills.",
    // budget_rebalance_service: `f"Freeing {-delta:.2f} of projected surplus"`
    suggestions: [{ ...OK.suggestions[0], reasoning: "Freeing 7373.37 of projected surplus" }, OK.suggestions[1]],
  } as never);
  render(<BudgetRebalanceModal open budgets={BUDGETS} onApplied={() => {}} onClose={() => {}} />);
  expect(await screen.findByText("Freeing 7373.37 of projected surplus")).toBeInTheDocument();
  expect(document.body.textContent).toMatch(/Move 7373\.37 from surplus/);
  try {
    act(() => setBalancesHidden(true));
    expect(document.body.textContent).not.toMatch(/7,?373/);
    expect(screen.getByText("Freeing ••••• of projected surplus")).toBeInTheDocument();
  } finally {
    act(() => setBalancesHidden(false));
  }
});
