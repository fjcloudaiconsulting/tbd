import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AIForecastRefineToggle, {
  type RefinedForecastResponse,
} from "@/components/dashboard/AIForecastRefineToggle";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/hooks/use-ai-status", () => ({
  useAiStatus: vi.fn(() => ({
    forecast: { entitled: true, configured: true },
    categorize: { entitled: true, configured: true },
    budget: { entitled: true, configured: true },
  })),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(() => ({ user: { role: "owner" } })),
}));

import { apiFetch, ApiResponseError } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";

const mockedFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

function refinedFixture(
  overrides?: Partial<RefinedForecastResponse>,
): RefinedForecastResponse {
  return {
    period_start: "2026-05-01",
    period_end: "2026-05-31",
    baseline_forecast_expense: "100.00",
    refined_forecast_expense: "120.00",
    baseline_forecast_income: "1000.00",
    refined_forecast_income: "1000.00",
    categories: [
      {
        category_id: 1,
        category_name: "Groceries",
        baseline_forecast: "100.00",
        multiplier: 1.2,
        refined_forecast: "120.00",
      },
    ],
    anomalies: [
      {
        category_id: 1,
        category_name: "Groceries",
        description: "Spike vs. trailing average.",
        severity: "warning",
      },
    ],
    provenance: {
      ai_applied: true,
      fallback_reason: null,
      model: "gpt-4o-mini",
      confidence: 0.75,
      summary: "Detected one mild seasonal uptick.",
      notes: [],
    },
    ...overrides,
  };
}

function estimateFixture(can_proceed = true) {
  return {
    est_prompt_tokens: 11000,
    est_output_tokens: 2000,
    est_cost_cents: 15,
    duration_band: "~20-40s",
    can_proceed,
    reason: can_proceed ? null : "ai_routing_not_configured",
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AIForecastRefineToggle - idle and apply flow", () => {
  it("renders the apply CTA when idle; clicking opens the configure panel", async () => {
    // First call is the estimate (on panel mount).
    mockedFetch.mockResolvedValue(estimateFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    const button = screen.getByTestId("ai-forecast-refine-toggle");
    expect(button).toBeInTheDocument();
    expect(button.textContent).toContain("Refine forecast with AI");

    fireEvent.click(button);

    // Panel opens; estimate call fires; Confirm button appears.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument(),
    );
    // Direct refine NOT called yet — only the estimate call.
    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/ai/forecast/refine/estimate",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("opens the review step after Confirm; nothing is reflected until Apply", async () => {
    // First call: estimate. Second call: refine.
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(refinedFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    // Wait for Confirm to be enabled.
    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    // The review modal opens with the per-row diff. The refined badge is
    // NOT yet shown — nothing applies until the user clicks Apply.
    await waitFor(() =>
      expect(screen.getByTestId("forecast-refine-diff-table")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("ai-forecast-refined-panel")).toBeNull();
    expect(screen.queryByTestId("ai-refined-badge")).toBeNull();
    // The adjusted category is listed for review, accepted by default
    // (seeded on first render by the lazy initializer).
    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", {
          name: /Apply adjustment for Groceries/i,
        }),
      ).toBeChecked(),
    );
    expect(screen.getByTestId("forecast-refine-row-1")).toHaveAttribute(
      "data-row-accepted",
      "yes",
    );
  });

  it("shows the AI-refined badge after the user accepts and applies", async () => {
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(refinedFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    // Accept-by-default is seeded on first render, so the row checkbox is
    // already checked before Apply.
    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", {
          name: /Apply adjustment for Groceries/i,
        }),
      ).toBeChecked(),
    );
    const applyBtn = await screen.findByTestId("forecast-refine-apply");
    fireEvent.click(applyBtn);

    await waitFor(() =>
      expect(screen.getByTestId("ai-forecast-refined-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("ai-refined-badge")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-fallback-badge")).toBeNull();
    // Delta vs. baseline surfaced.
    expect(screen.getByText(/\+20\.00 vs\. baseline/)).toBeInTheDocument();
    expect(screen.getByText(/1 category adjusted/)).toBeInTheDocument();
    // Refine endpoint hit exactly once — Apply does NOT re-call the AI.
    const refineCalls = mockedFetch.mock.calls.filter(
      (c) => c[0] === "/api/v1/ai/forecast/refine",
    );
    expect(refineCalls).toHaveLength(1);
  });

  it("skipping a row reverts that category to baseline (zero net delta)", async () => {
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(refinedFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    // Skip the single adjustment, then apply. The checkbox is checked on
    // first render (lazy initializer), so we can uncheck it directly.
    const checkbox = await screen.findByRole("checkbox", {
      name: /Apply adjustment for Groceries/i,
    });
    await waitFor(() => expect(checkbox).toBeChecked());
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByTestId("forecast-refine-apply"));

    await waitFor(() =>
      expect(screen.getByTestId("ai-forecast-refined-panel")).toBeInTheDocument(),
    );
    // With the only adjustment skipped, the displayed delta is zero and
    // no categories count as adjusted.
    expect(screen.getByText(/\+0\.00 vs\. baseline/)).toBeInTheDocument();
    expect(screen.queryByText(/category adjusted/)).toBeNull();
  });

  it("Cancel on the review modal discards the result (back to idle)", async () => {
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(refinedFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    await screen.findByTestId("forecast-refine-diff-table");
    fireEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));

    // Back to the idle CTA, nothing reflected.
    await waitFor(() =>
      expect(screen.getByTestId("ai-forecast-refine-toggle")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("ai-refined-badge")).toBeNull();
  });
});

describe("AIForecastRefineToggle - tooltip details", () => {
  it("toggles the detail panel and lists adjustments + anomalies", async () => {
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(refinedFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    // Accept the adjustments in the review step to reach the refined view.
    // The checkbox is checked on first render (lazy initializer).
    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", {
          name: /Apply adjustment for Groceries/i,
        }),
      ).toBeChecked(),
    );
    const applyBtn = await screen.findByTestId("forecast-refine-apply");
    fireEvent.click(applyBtn);

    await waitFor(() =>
      expect(screen.getByTestId("ai-forecast-refined-panel")).toBeInTheDocument(),
    );

    // Closed by default.
    expect(screen.queryByTestId("ai-adjustments-list")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /What changed/i }));

    await waitFor(() =>
      expect(screen.getByTestId("ai-adjustments-list")).toBeInTheDocument(),
    );
    // Summary shown in the tooltip.
    expect(
      screen.getByText("Detected one mild seasonal uptick."),
    ).toBeInTheDocument();
    // Adjustment row lists the multiplier as x1.20.
    const list = screen.getByTestId("ai-adjustments-list");
    expect(list.textContent).toContain("Groceries");
    expect(list.textContent).toContain("x1.20");
    // Anomaly listed — match the description via a text-content includes
    // check because the severity tag renders as a sibling span.
    expect(screen.getByText(/Spike vs\. trailing average/i)).toBeInTheDocument();
  });
});

describe("AIForecastRefineToggle - fallback handling", () => {
  it("shows fallback badge + reason when backend returns ai_applied=false", async () => {
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(
        refinedFixture({
          refined_forecast_expense: "100.00",
          provenance: {
            ai_applied: false,
            fallback_reason: "ai_routing_not_configured",
            model: null,
            confidence: null,
            summary: null,
            notes: [],
          },
          categories: [
            {
              category_id: 1,
              category_name: "Groceries",
              baseline_forecast: "100.00",
              multiplier: 1.0,
              refined_forecast: "100.00",
            },
          ],
          anomalies: [],
        }),
      );

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(screen.getByTestId("ai-fallback-badge")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("ai-refined-badge")).toBeNull();
    // Friendly copy, never the raw code.
    const reasonText = screen.getByTestId("ai-fallback-reason").textContent ?? "";
    expect(reasonText).toContain("Configure an AI provider");
    expect(reasonText).not.toContain("ai_routing_not_configured");
  });

  it("hides itself entirely when the estimate call returns a 403 (feature gate closed)", async () => {
    mockedFetch.mockRejectedValue(
      new ApiResponseError(403, "feature_not_enabled", "feature_not_enabled"),
    );

    const { container } = render(
      <AIForecastRefineToggle periodStart="2026-05-01" />,
    );
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    await waitFor(() => {
      // After 403 the toggle removes itself from the DOM.
      expect(container.firstChild).toBeNull();
    });
  });
});

describe("AIForecastRefineToggle - visibility prop", () => {
  it("renders nothing when visible=false", () => {
    const { container } = render(
      <AIForecastRefineToggle periodStart="2026-05-01" visible={false} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("AIForecastRefineToggle - AI status gating", () => {
  it("shows Set up AI CTA (not the toggle) when entitled but not configured", () => {
    (useAiStatus as ReturnType<typeof vi.fn>).mockReturnValueOnce({
      forecast: { entitled: true, configured: false },
      categorize: { entitled: true, configured: true },
      budget: { entitled: true, configured: true },
    });

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);

    expect(screen.queryByTestId("ai-forecast-refine-toggle")).toBeNull();
    expect(screen.getByText(/set up ai/i)).toBeInTheDocument();
  });
});
