// BudgetRebalanceModal — zero-sum free-allocation rebalance (TBD-461).
//
// The modal lets the user move amounts freely between budgets; Apply is
// enabled only once every row parses and the net change is exactly zero.
// Rows render from a snapshot taken on open, never from the live `budgets`
// prop, so a background reload mid-edit cannot silently add/hide a row.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

import BudgetRebalanceModal from "@/components/budgets/BudgetRebalanceModal";
import { apiFetch, ApiResponseError } from "@/lib/api";
import { setBalancesHidden } from "@/lib/format";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const BUDGETS = [
  { id: 11, category_id: 1, category_name: "Transportation", amount: 100 },
  { id: 12, category_id: 2, category_name: "Groceries", amount: 90 },
];

function renderModal(props: Partial<ComponentProps<typeof BudgetRebalanceModal>> = {}) {
  return render(
    <BudgetRebalanceModal
      open
      budgets={BUDGETS}
      canSuggest={false}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onClose={vi.fn()}
      {...props}
    />,
  );
}

function amountInput(name: string) {
  return screen.getByRole("textbox", { name: `${name} amount` }) as HTMLInputElement;
}

function sliderInput(name: string) {
  return screen.getByRole("slider", { name: `${name} allocation` }) as HTMLInputElement;
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  setBalancesHidden(false);
});

it("F-F1: sums deltas as integer cents, not floats", async () => {
  renderModal({
    budgets: [
      { id: 1, category_id: 1, category_name: "A", amount: "0.10" },
      { id: 2, category_id: 2, category_name: "B", amount: "0.20" },
      { id: 3, category_id: 3, category_name: "C", amount: "0.30" },
    ],
  });
  fireEvent.change(amountInput("A"), { target: { value: "0.20" } });
  fireEvent.change(amountInput("B"), { target: { value: "0.40" } });
  fireEvent.change(amountInput("C"), { target: { value: "0.00" } });
  // +0.10 / +0.20 / -0.30 nets to zero.
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();

  // Leave a +0.01 residue: no longer balanced.
  fireEvent.change(amountInput("C"), { target: { value: "0.01" } });
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
});

it("F-F2: Apply sends exactly one POST to /budgets/rebalance with expected_amount, no PUTs", async () => {
  vi.mocked(apiFetch).mockResolvedValue([] as never);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const onClose = vi.fn();
  renderModal({ onApplied, onClose });

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(apiFetch).toHaveBeenCalledTimes(1);
  expect(apiFetch).toHaveBeenCalledWith(
    "/api/v1/budgets/rebalance",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        items: [
          { budget_id: 11, expected_amount: "100.00", amount: "90.00" },
          { budget_id: 12, expected_amount: "90.00", amount: "100.00" },
        ],
      }),
    }),
  );
  const putCalls = vi.mocked(apiFetch).mock.calls.filter(
    (c) => (c[1] as RequestInit | undefined)?.method === "PUT",
  );
  expect(putCalls).toHaveLength(0);
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("F-F3: slider and text input stay in sync", async () => {
  renderModal();
  const slider = sliderInput("Transportation");
  fireEvent.change(slider, { target: { value: "75" } });
  expect(amountInput("Transportation").value).toBe("75.00");

  fireEvent.change(amountInput("Transportation"), { target: { value: "60" } });
  expect(slider.value).toBe("60");
});

it("F-F4: no AI call on open; suggestions absent when canSuggest is false; fills only mapped rows on click; surfaces a non-ok status", async () => {
  const { rerender } = renderModal({ canSuggest: false });
  expect(apiFetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: /use suggestions/i })).toBeNull();
  // Modal still fully works without suggestions.
  expect(amountInput("Transportation")).toBeInTheDocument();

  vi.mocked(apiFetch).mockResolvedValue({
    status: "ok",
    period_start: "2026-06-01",
    summary: "Shift to groceries",
    suggestions: [
      {
        category_id: 1,
        category_name: "Transportation",
        current_amount: 100,
        suggested_amount: 80,
        delta_amount: -20,
        reasoning: "surplus",
      },
    ],
  } as never);
  rerender(
    <BudgetRebalanceModal
      open
      budgets={BUDGETS}
      canSuggest
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onClose={vi.fn()}
    />,
  );
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /use suggestions/i }));
  await waitFor(() => expect(amountInput("Transportation").value).toBe("80.00"));
  // Unmapped row untouched.
  expect(amountInput("Groceries").value).toBe("90.00");

  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockResolvedValue({
    status: "empty_no_surplus",
    period_start: "2026-06-01",
    summary: "Every category is at or over budget.",
    suggestions: [],
  } as never);
  fireEvent.click(screen.getByRole("button", { name: /use suggestions/i }));
  await waitFor(() =>
    expect(screen.getByText(/every category is at or over budget/i)).toBeInTheDocument(),
  );
});

it("F-F5: 409 keeps the modal open, awaits onApplied, and rebuilds from fresh props", async () => {
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const onClose = vi.fn();
  const { rerender } = renderModal({ onApplied, onClose });

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });

  const err = new ApiResponseError(409, "Budgets changed since you opened this.", "budget_changed");
  vi.mocked(apiFetch).mockRejectedValueOnce(err);
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() =>
    expect(screen.getByText(/budgets changed since you opened this/i)).toBeInTheDocument(),
  );
  expect(onClose).not.toHaveBeenCalled();
  expect(onApplied).toHaveBeenCalledTimes(1);

  // Parent re-renders with fresh amounts (as if it reloaded).
  rerender(
    <BudgetRebalanceModal
      open
      budgets={[
        { id: 11, category_id: 1, category_name: "Transportation", amount: 95 },
        { id: 12, category_id: 2, category_name: "Groceries", amount: 95 },
      ]}
      canSuggest={false}
      onApplied={onApplied}
      onClose={onClose}
    />,
  );

  await waitFor(() => expect(amountInput("Transportation").value).toBe("95.00"));
  expect(amountInput("Groceries").value).toBe("95.00");
});

it("F-F6: a mid-edit prop change (including an added budget) does not reset typed values or add a row", async () => {
  const { rerender } = renderModal();
  fireEvent.change(amountInput("Transportation"), { target: { value: "77.00" } });

  rerender(
    <BudgetRebalanceModal
      open
      budgets={[...BUDGETS, { id: 13, category_id: 3, category_name: "New", amount: 5 }]}
      canSuggest={false}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onClose={vi.fn()}
    />,
  );

  expect(amountInput("Transportation").value).toBe("77.00");
  expect(screen.queryByText("New")).toBeNull();
});

it("F-F7: parses '12.' and '.5'; rejects '' and '1.005' with an inline error", async () => {
  renderModal();
  const txField = amountInput("Transportation");

  fireEvent.change(txField, { target: { value: "12." } });
  expect(sliderInput("Transportation").value).toBe("12");

  fireEvent.change(txField, { target: { value: ".5" } });
  expect(sliderInput("Transportation").value).toBe("0.5");

  fireEvent.change(txField, { target: { value: "" } });
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
  expect(txField).toHaveAttribute("aria-invalid", "true");
  expect(screen.getAllByText(/use a number with up to 2 decimals/i).length).toBeGreaterThan(0);

  fireEvent.change(txField, { target: { value: "1.005" } });
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
  expect(txField).toHaveAttribute("aria-invalid", "true");
});

it("F-F9: hidden balances replace the body with a prompt and leak no digits", async () => {
  setBalancesHidden(true);
  renderModal();
  expect(screen.getByText("Show balances to rebalance.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /amount/i })).toBeNull();
  expect(screen.queryByRole("slider")).toBeNull();
  expect(document.body.textContent).not.toMatch(/\d/);
  expect(screen.getByRole("button", { name: /cancel/i })).toBeEnabled();

  fireEvent.click(screen.getByRole("button", { name: /show balances/i }));
  await waitFor(() => expect(amountInput("Transportation")).toBeInTheDocument());
  setBalancesHidden(false);
});

it("F-F10: slider and text input each carry the category's own accessible name", async () => {
  renderModal();
  expect(sliderInput("Groceries")).toBeInTheDocument();
  expect(amountInput("Groceries")).toBeInTheDocument();
});

it("G-F11: the status region changes text only on a state flip; Escape closes", async () => {
  const onClose = vi.fn();
  renderModal({ onClose });
  const status = screen.getByRole("status");
  expect(status).toHaveTextContent("No changes yet");

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  expect(status).toHaveTextContent(/not balanced/i);
  const textAfterFirst = status.textContent;

  // Drag across two different non-zero nets: still "not balanced", same text.
  fireEvent.change(amountInput("Transportation"), { target: { value: "80.00" } });
  expect(status.textContent).toBe(textAfterFirst);

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  expect(status).toHaveTextContent(/^balanced$/i);

  fireEvent.keyDown(document, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
});
