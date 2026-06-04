import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AIForecastRefineToggle, {
  type RefinedForecastResponse,
} from "@/components/dashboard/AIForecastRefineToggle";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch, ApiResponseError } from "@/lib/api";

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
    expect(button.textContent).toContain("Apply AI refinement");

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

  it("shows the AI-refined badge after Confirm completes", async () => {
    // First call: estimate. Second call: refine.
    mockedFetch
      .mockResolvedValueOnce(estimateFixture())
      .mockResolvedValueOnce(refinedFixture());

    render(<AIForecastRefineToggle periodStart="2026-05-01" />);
    fireEvent.click(screen.getByTestId("ai-forecast-refine-toggle"));

    // Wait for Confirm to be enabled.
    const confirmBtn = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(screen.getByTestId("ai-forecast-refined-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("ai-refined-badge")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-fallback-badge")).toBeNull();
    // Delta vs. baseline surfaced.
    expect(screen.getByText(/\+20\.00 vs\. baseline/)).toBeInTheDocument();
    expect(screen.getByText(/1 category adjusted/)).toBeInTheDocument();
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
    expect(screen.getByTestId("ai-fallback-reason").textContent).toContain(
      "ai_routing_not_configured",
    );
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
