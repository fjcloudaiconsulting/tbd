import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch } from "@/lib/api";
import { AIForecastRefinePanel } from "@/components/dashboard/AIForecastRefinePanel";

const mockedFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockedFetch.mockReset();
  vi.restoreAllMocks();
});

describe("AIForecastRefinePanel", () => {
  it("shows the estimated cost and enables Confirm when can_proceed", async () => {
    mockedFetch.mockResolvedValue({
      est_prompt_tokens: 11000,
      est_output_tokens: 2000,
      est_cost_cents: 15,
      duration_band: "~20-40s",
      can_proceed: true,
      reason: null,
    });

    render(<AIForecastRefinePanel onApplied={() => {}} />);

    await waitFor(() => expect(screen.getByText(/\$0\.15/)).toBeInTheDocument());
    expect(screen.getByText(/~20-40s/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /confirm/i })).toBeEnabled();
  });

  it("disables Confirm and shows the reason when can_proceed is false", async () => {
    mockedFetch.mockResolvedValue({
      est_prompt_tokens: 0,
      est_output_tokens: 0,
      est_cost_cents: 0,
      duration_band: "~20-40s",
      can_proceed: false,
      reason: "ai_routing_not_configured",
    });

    render(<AIForecastRefinePanel onApplied={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /confirm/i })).toBeDisabled(),
    );
    // friendly reason copy mentions "provider"
    expect(screen.getByText(/provider/i)).toBeInTheDocument();
  });
});
