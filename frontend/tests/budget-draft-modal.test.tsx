// BudgetDraftModal — next-period draft review, create-on-apply.
//
// Unlike the rebalance modal (which PUTs existing budgets), the draft
// applies by POSTing new budgets into the next period. No balance meter
// (a draft is not conservation-constrained).

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import BudgetDraftModal from "@/components/budgets/BudgetDraftModal";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const DRAFT_OK = {
  status: "ok",
  period_start: "2026-08-01",
  summary: "Draft budgets projected from your last 3 months of spending.",
  suggestions: [
    {
      category_id: 1,
      category_name: "Groceries",
      current_amount: 0,
      suggested_amount: 300,
      delta_amount: 300,
      reasoning: "Based on about 300.00 per month over the last 3 months.",
    },
    {
      category_id: 2,
      category_name: "Dining",
      current_amount: 0,
      suggested_amount: 120,
      delta_amount: 120,
      reasoning: "Based on about 120.00 per month over the last 3 months.",
    },
  ],
};

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
});

it("renders draft suggestions with no balance meter", async () => {
  vi.mocked(apiFetch).mockResolvedValue(DRAFT_OK as never);
  render(
    <BudgetDraftModal
      open
      periodStart="2026-08-01"
      onApplied={() => {}}
      onClose={() => {}}
    />,
  );
  expect(await screen.findByText("Groceries")).toBeInTheDocument();
  expect(screen.getByText("Dining")).toBeInTheDocument();
  // A draft is not zero-sum; there must be NO balance meter.
  expect(screen.queryByTestId("rebalance-balance-meter")).toBeNull();
});

it("applies by POSTing new budgets into the next period", async () => {
  vi.mocked(apiFetch)
    .mockResolvedValueOnce(DRAFT_OK as never) // initial draft fetch
    .mockResolvedValue({} as never); // per-row create
  const onApplied = vi.fn();
  render(
    <BudgetDraftModal
      open
      periodStart="2026-08-01"
      onApplied={onApplied}
      onClose={() => {}}
    />,
  );
  const applyBtn = await screen.findByRole("button", { name: /apply/i });
  fireEvent.click(applyBtn);

  await waitFor(() => expect(onApplied).toHaveBeenCalled());
  // Two create calls, each a POST to the period-scoped budgets endpoint.
  const createCalls = vi
    .mocked(apiFetch)
    .mock.calls.filter(
      ([url, opts]) =>
        String(url).includes("/api/v1/budgets?period_start=2026-08-01") &&
        (opts as RequestInit | undefined)?.method === "POST",
    );
  expect(createCalls).toHaveLength(2);
});

it("shows an empty state when there is no history to draft from", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "empty_no_history",
    period_start: "2026-08-01",
    summary: "Not enough recent spending history to draft a budget.",
    suggestions: [],
  } as never);
  render(
    <BudgetDraftModal
      open
      periodStart="2026-08-01"
      onApplied={() => {}}
      onClose={() => {}}
    />,
  );
  await waitFor(() =>
    expect(screen.getByTestId("draft-empty-state")).toHaveTextContent(
      /not enough recent spending history/i,
    ),
  );
});
