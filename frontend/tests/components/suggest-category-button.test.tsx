/**
 * LAI.1 — SuggestCategoryButton component tests.
 *
 * Pins:
 * - Click invokes POST /api/v1/ai/categorize with the given transaction_id.
 * - On success, onSuggested is called with the parsed payload.
 * - On 4xx/5xx, the component shows an inline error and does NOT call
 *   onSuggested (soft-fail).
 * - The confidence chip appears after a successful suggestion.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SuggestCategoryButton from "@/components/transactions/SuggestCategoryButton";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

describe("SuggestCategoryButton", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("calls the categorize endpoint and surfaces the suggestion", async () => {
    apiFetchMock.mockResolvedValueOnce({
      transaction_id: 42,
      category_id: 7,
      category_name: "Groceries",
      confidence: 0.83,
      reasoning: "Whole Foods Market resembles a grocery purchase.",
    } as never);

    const onSuggested = vi.fn();
    render(
      <SuggestCategoryButton transactionId={42} onSuggested={onSuggested} />,
    );

    fireEvent.click(screen.getByTestId("ai-suggest-button"));

    await waitFor(() => expect(onSuggested).toHaveBeenCalledTimes(1));

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/ai/categorize",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ transaction_id: 42 }),
      }),
    );
    expect(onSuggested).toHaveBeenCalledWith(
      expect.objectContaining({
        category_id: 7,
        category_name: "Groceries",
        confidence: 0.83,
      }),
    );
    const chip = await screen.findByTestId("ai-suggest-confidence");
    expect(chip).toHaveTextContent("Groceries");
    expect(chip).toHaveTextContent("83% confidence");
  });

  it("renders an error and does not call onSuggested when the API fails", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("503 service unavailable"));

    const onSuggested = vi.fn();
    render(
      <SuggestCategoryButton transactionId={1} onSuggested={onSuggested} />,
    );

    fireEvent.click(screen.getByTestId("ai-suggest-button"));

    const errEl = await screen.findByTestId("ai-suggest-error");
    expect(errEl.textContent).toMatch(/service unavailable|Couldn't/);
    expect(onSuggested).not.toHaveBeenCalled();
    // No confidence chip rendered on failure.
    expect(screen.queryByTestId("ai-suggest-confidence")).toBeNull();
  });

  it("respects the disabled prop", () => {
    render(
      <SuggestCategoryButton
        transactionId={1}
        onSuggested={vi.fn()}
        disabled
      />,
    );
    expect(screen.getByTestId("ai-suggest-button")).toBeDisabled();
  });
});
