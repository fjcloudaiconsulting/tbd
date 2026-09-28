// BudgetRebalanceModal — zero-sum free-allocation rebalance (TBD-461).
//
// The modal lets the user move amounts freely between budgets; Apply is
// enabled only once every row parses and the net change is exactly zero.
// Rows render from a snapshot taken on open, never from the live `budgets`
// prop, so a background reload mid-edit cannot silently add/hide a row.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
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

it("F-F1b: a float TOTAL compare also fails here (0.30 - 0.10 !== -(0.00 - 0.20) in binary float)", async () => {
  renderModal({
    budgets: [
      { id: 1, category_id: 1, category_name: "A", amount: "0.10" },
      { id: 2, category_id: 2, category_name: "B", amount: "0.20" },
    ],
  });
  fireEvent.change(amountInput("A"), { target: { value: "0.30" } });
  fireEvent.change(amountInput("B"), { target: { value: "0.00" } });
  // +0.20 / -0.20 nets to exactly zero in cents; a float sum of
  // (0.30 - 0.10) + (0.00 - 0.20) is off by ~2e-17 and would wrongly disable.
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();
});

it("F-F2: Apply sends exactly one POST to /budgets/rebalance with expected_amount, no PUTs, and no untouched row", async () => {
  vi.mocked(apiFetch).mockResolvedValue([] as never);
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const onClose = vi.fn();
  renderModal({
    onApplied,
    onClose,
    budgets: [...BUDGETS, { id: 13, category_id: 3, category_name: "Rent", amount: 1000 }],
  });

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  expect(apiFetch).toHaveBeenCalledTimes(1);
  const [url, opts] = vi.mocked(apiFetch).mock.calls[0];
  expect(url).toBe("/api/v1/budgets/rebalance");
  expect((opts as RequestInit).method).toBe("POST");
  expect(JSON.parse((opts as RequestInit).body as string)).toEqual({
    items: [
      { budget_id: 11, expected_amount: "100.00", amount: "90.00" },
      { budget_id: 12, expected_amount: "90.00", amount: "100.00" },
    ],
  });
  const putCalls = vi.mocked(apiFetch).mock.calls.filter(
    (c) => (c[1] as RequestInit | undefined)?.method === "PUT",
  );
  expect(putCalls).toHaveLength(0);
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("C5: POST succeeds but onApplied() rejects — still closes, not shown as an Apply failure", async () => {
  vi.mocked(apiFetch).mockResolvedValue([] as never);
  const onApplied = vi.fn().mockRejectedValue(new Error("reload failed"));
  const onClose = vi.fn();
  renderModal({ onApplied, onClose });

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("alert")).toBeNull();
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
  expect(screen.queryByRole("button", { name: /^use suggestions$/i })).toBeNull();
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
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));
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
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));
  await waitFor(() =>
    expect(screen.getByText(/every category is at or over budget/i)).toBeInTheDocument(),
  );
});

it("V1: no AI fetch on a FRESH mount with canSuggest already true (kills fetch-on-open)", async () => {
  renderModal({ canSuggest: true });
  // Flush any pending effects/microtasks before asserting silence.
  await waitFor(() => expect(amountInput("Transportation")).toBeInTheDocument());
  expect(apiFetch).not.toHaveBeenCalled();
});

it("V3: 'Use suggestions' fills only mapped rows, preserving a row the user already edited", async () => {
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
  renderModal({ canSuggest: true });

  fireEvent.change(amountInput("Groceries"), { target: { value: "95.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));

  await waitFor(() => expect(amountInput("Transportation").value).toBe("80.00"));
  // The preset must not clobber a row the user already typed into.
  expect(amountInput("Groceries").value).toBe("95.00");
  // A preset that DID match must not also claim that nothing matched.
  expect(screen.queryByText(/no suggestions matched/i)).toBeNull();
});

it("C4: an ok response with no suggestion mapped to any row shows an inline note", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "ok",
    period_start: "2026-06-01",
    summary: "",
    suggestions: [
      {
        category_id: 999,
        category_name: "Unrelated",
        current_amount: 10,
        suggested_amount: 5,
        delta_amount: -5,
        reasoning: "n/a",
      },
    ],
  } as never);
  renderModal({ canSuggest: true });
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));

  await waitFor(() =>
    expect(screen.getByText(/no suggestions matched a budget in this period/i)).toBeInTheDocument(),
  );
  // Unmapped rows stay at base.
  expect(amountInput("Transportation").value).toBe("100.00");
  expect(amountInput("Groceries").value).toBe("90.00");
});

// A WORST-CASE parent: it commits the reloaded `budgets` (and flushes that
// commit's effects) before `onApplied` resolves, then never renders again.
// The real page's `loadBudgets` only queues `setBudgets`, so production is
// usually kinder than this; the modal must be correct under both orderings,
// and only this one separates a "wait for the next `budgets` change" ref from
// the state token.
function renderModalWithCommittingParent(
  status: 409 | 404,
  onApplied: (budgets: typeof BUDGETS) => Promise<void> | void = () => {},
) {
  const err = new ApiResponseError(status, "Budgets changed since you opened this.", "budget_changed");
  vi.mocked(apiFetch).mockRejectedValueOnce(err);
  const onClose = vi.fn();
  const reloaded = [
    { id: 11, category_id: 1, category_name: "Transportation", amount: 95 },
    { id: 12, category_id: 2, category_name: "Groceries", amount: 95 },
  ];

  function Wrapper() {
    const [budgets, setBudgets] = useState(BUDGETS);
    const handleApplied = async () => {
      // `act` flushes the commit AND runs its passive effects for this
      // `budgets` change before control returns here — deterministically,
      // rather than hoping a macrotask wins a scheduling race. This is what
      // makes a ref armed *after* this point ("wait for the NEXT `budgets`
      // change") permanently miss it: the only commit that ever changes
      // `budgets` has already had its effects run, with the ref still false.
      await act(async () => {
        setBudgets(reloaded);
      });
      return onApplied(reloaded);
    };
    return (
      <BudgetRebalanceModal
        open
        budgets={budgets}
        canSuggest={false}
        onApplied={handleApplied}
        onClose={onClose}
      />
    );
  }
  render(<Wrapper />);
  return { onClose };
}

it("F-F5 / K1: a 409 reconciles from the parent's synchronous onApplied commit (no later render), and C1 keeps the reload message visible", async () => {
  const { onClose } = renderModalWithCommittingParent(409);

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() => expect(amountInput("Transportation").value).toBe("95.00"));
  expect(amountInput("Groceries").value).toBe("95.00");
  expect(onClose).not.toHaveBeenCalled();
  // C1: the re-snapshot must not erase the reconcile message it is reporting on.
  expect(screen.getByText(/reloaded the latest amounts/i)).toBeInTheDocument();
});

it("F-F5b: 404 is treated the same as 409 (reload + re-snapshot)", async () => {
  const { onClose } = renderModalWithCommittingParent(404);

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() => expect(amountInput("Transportation").value).toBe("95.00"));
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByText(/reloaded the latest amounts/i)).toBeInTheDocument();
});

it("F-F5c: a rejecting onApplied on the reconcile path shows 'Could not reload'", async () => {
  const onApplied = vi.fn().mockRejectedValue(new Error("network down"));
  const onClose = vi.fn();
  renderModal({ onApplied, onClose });

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });

  const err = new ApiResponseError(409, "Budgets changed since you opened this.", "budget_changed");
  vi.mocked(apiFetch).mockRejectedValueOnce(err);
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

  await waitFor(() =>
    expect(screen.getByText(/could not reload the latest amounts/i)).toBeInTheDocument(),
  );
  expect(onClose).not.toHaveBeenCalled();
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

it("F-F7: parses '12.', '.5' and '12.5'; rejects '' and '1.005' with an inline error", async () => {
  renderModal();
  const txField = amountInput("Transportation");

  fireEvent.change(txField, { target: { value: "12." } });
  expect(sliderInput("Transportation").value).toBe("12");

  fireEvent.change(txField, { target: { value: ".5" } });
  expect(sliderInput("Transportation").value).toBe("0.5");

  fireEvent.change(txField, { target: { value: "12.5" } });
  expect(sliderInput("Transportation").value).toBe("12.5");

  // Groceries set so the net would be exactly zero if "" parsed as 0
  // (base 100 -> "" and base 90 -> 190 both delta by -100/+100): this makes
  // `toBeDisabled` below discriminate a missing `allValid` guard, not just a
  // nonzero net.
  fireEvent.change(amountInput("Groceries"), { target: { value: "190.00" } });
  fireEvent.change(txField, { target: { value: "" } });
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
  expect(txField).toHaveAttribute("aria-invalid", "true");
  expect(screen.getAllByText(/use a number with up to 2 decimals/i).length).toBeGreaterThan(0);

  fireEvent.change(txField, { target: { value: "1.005" } });
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
  expect(txField).toHaveAttribute("aria-invalid", "true");
});

it("C3: an 11-digit whole part is invalid (Numeric(12,2) caps at 10 integer digits)", async () => {
  renderModal();
  const txField = amountInput("Transportation");
  fireEvent.change(txField, { target: { value: "12345678901" } });
  expect(txField).toHaveAttribute("aria-invalid", "true");

  fireEvent.change(txField, { target: { value: "1234567890" } });
  expect(txField).not.toHaveAttribute("aria-invalid");
});

it("C2: comparing cents, not text — '12.5' typed over a '12.50' base is not a change", async () => {
  renderModal({
    budgets: [{ id: 1, category_id: 1, category_name: "A", amount: "12.50" }],
  });
  fireEvent.change(amountInput("A"), { target: { value: "12.5" } });
  expect(screen.getByRole("status")).toHaveTextContent("No changes yet");
  expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
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

it("R1: renders the AI summary in a quiet panel above rows after suggestions load", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "ok",
    period_start: "2026-06-01",
    summary: "Shift 7373.37 to groceries",
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
  renderModal({ canSuggest: true });
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));

  const summary = await screen.findByTestId("rebalance-summary");
  expect(summary).toHaveTextContent("Shift 7373.37 to groceries");

  // Masked when balances are hidden.
  act(() => setBalancesHidden(true));
  expect(screen.queryByTestId("rebalance-summary")).toBeNull(); // body hidden entirely
  act(() => setBalancesHidden(false));
});

it("R2: shows the old uncovered-overspend banner exactly when uncovered_overspend > 0", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "ok",
    period_start: "2026-06-01",
    summary: "",
    uncovered_overspend: 30,
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
  renderModal({ canSuggest: true });
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));

  const banner = await screen.findByTestId("rebalance-uncovered");
  expect(banner).toHaveTextContent(/over plan/i);

  // Reset the mock to uncovered_overspend: 0 and re-fetch: banner must go away.
  vi.mocked(apiFetch).mockResolvedValue({
    status: "ok",
    period_start: "2026-06-01",
    summary: "",
    uncovered_overspend: 0,
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
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));
  await waitFor(() => expect(screen.queryByTestId("rebalance-uncovered")).toBeNull());
});

it("R3: shows each row's reasoning under it, and clears it on Reset and on re-snapshot", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "ok",
    period_start: "2026-06-01",
    summary: "",
    suggestions: [
      {
        category_id: 1,
        category_name: "Transportation",
        current_amount: 100,
        suggested_amount: 80,
        delta_amount: -20,
        reasoning: "Freeing 7373.37 of projected surplus",
      },
    ],
  } as never);
  renderModal({ canSuggest: true });
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));

  await screen.findByText("Freeing 7373.37 of projected surplus");
  // Row without a suggestion (Groceries) shows nothing extra.
  expect(screen.queryByText(/n\/a/i)).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: /^reset$/i }));
  expect(screen.queryByText("Freeing 7373.37 of projected surplus")).toBeNull();
});

it("R4: uses the friendly empty-state mapping instead of a raw status string, and rows stay usable", async () => {
  vi.mocked(apiFetch).mockResolvedValue({
    status: "llm_unavailable",
    period_start: "2026-06-01",
    summary: "raw backend detail nobody should read verbatim",
    suggestions: [],
  } as never);
  renderModal({ canSuggest: true });
  fireEvent.click(screen.getByRole("button", { name: /^use suggestions$/i }));

  await waitFor(() => expect(screen.getByText(/ai is unavailable/i)).toBeInTheDocument());
  expect(screen.getByText(/raw backend detail nobody should read verbatim/i)).toBeInTheDocument();
  // Rows stay usable: the user can still allocate by hand.
  expect(amountInput("Transportation")).toBeEnabled();
});

it("F-F5d: a close and reopen while the 409 reload is in flight keeps the new session's edits", async () => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  vi.mocked(apiFetch).mockRejectedValueOnce(
    new ApiResponseError(409, "Budgets changed since you opened this.", "budget_changed"),
  );
  const props = { budgets: BUDGETS, canSuggest: false, onApplied: () => pending, onClose: () => {} };
  const { rerender } = render(<BudgetRebalanceModal open {...props} />);

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
  await waitFor(() => expect(screen.getByText(/reloaded the latest amounts/i)).toBeInTheDocument());

  rerender(<BudgetRebalanceModal open={false} {...props} />);
  rerender(<BudgetRebalanceModal open {...props} />);
  fireEvent.change(amountInput("Transportation"), { target: { value: "70.00" } });

  await act(async () => {
    release();
    await pending;
  });

  // The stale reload must not re-snapshot over the fresh session.
  expect(amountInput("Transportation").value).toBe("70.00");
  expect(screen.queryByText(/reloaded the latest amounts/i)).toBeNull();
});

it("Enter in an amount field applies a balanced change, and does nothing while unbalanced (ticket DoD: Enter submits)", async () => {
  vi.mocked(apiFetch).mockResolvedValue([] as never);
  renderModal();

  fireEvent.change(amountInput("Transportation"), { target: { value: "90.00" } });
  fireEvent.keyDown(amountInput("Transportation"), { key: "Enter" });
  expect(apiFetch).not.toHaveBeenCalled();

  fireEvent.change(amountInput("Groceries"), { target: { value: "100.00" } });
  fireEvent.keyDown(amountInput("Groceries"), { key: "Enter" });
  await waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/budgets/rebalance", expect.objectContaining({ method: "POST" })),
  );
});
