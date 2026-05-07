import { fireEvent, render, screen } from "@testing-library/react";

import OnTrackTile from "@/components/dashboard/OnTrackTile";

const PLAN_1000 = { total_planned_expense: "1000" };

function defaults(overrides: Partial<Parameters<typeof OnTrackTile>[0]> = {}) {
  return {
    forecastPlan: null,
    projection: null,
    projectionFailed: false,
    projectionLoading: false,
    onRetryProjection: vi.fn(),
    isPastPeriod: false,
    isFuturePeriod: false,
    ...overrides,
  };
}

describe("OnTrackTile — verdict thresholds (current period, anchored on actuals)", () => {
  it("renders ON TRACK when executed/plan <= 0.95", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "300", forecast_expense: "900" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/ON TRACK/);
    expect(screen.getByRole("heading", { level: 2 })).not.toHaveTextContent(/WATCH/);
    expect(screen.getByRole("heading", { level: 2 })).not.toHaveTextContent(/OVER BUDGET/);
  });

  it("renders WATCH when 0.95 < executed/plan <= 1.05", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "1000", forecast_expense: "1000" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^WATCH/);
  });

  it("renders OVER BUDGET when executed/plan > 1.05", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "1200", forecast_expense: "1200" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/OVER BUDGET/);
  });
});

describe("OnTrackTile — verdict ignores projection (the user-reported bug)", () => {
  // Bug: a fully-pending month (executed=0) used to read as OVER BUDGET because
  // the projected expense (settled + pending + remaining recurring fires)
  // exceeded the plan. The verdict now anchors on settled spending only.

  it("ON TRACK when nothing has actually been spent yet, even with projected > plan", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: { total_planned_expense: "561.86" },
          projection: { executed_expense: "0", forecast_expense: "1050" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^ON TRACK/);
    // Projected stat still shows the number, but muted (informational only).
    const projectedLabel = screen.getByText(/^PROJECTED$/);
    const projectedValue = projectedLabel.parentElement?.querySelectorAll("p")[1];
    expect(projectedValue?.textContent).toMatch(/1,050/);
    expect(projectedValue?.className).toMatch(/text-text-muted/);
  });

  it("OVER BUDGET when settled spending alone exceeds the plan", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: { total_planned_expense: "561.86" },
          projection: { executed_expense: "600", forecast_expense: "600" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^OVER BUDGET/);
  });

  it("ON TRACK with projected over plan: variance is favorable, no danger color anywhere", () => {
    const { container } = render(
      <OnTrackTile
        {...defaults({
          forecastPlan: { total_planned_expense: "561.86" },
          projection: { executed_expense: "300", forecast_expense: "900" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^ON TRACK/);
    // Variance under plan, accent (favorable) color, no danger.
    expect(screen.getByText(/under plan/i)).toBeInTheDocument();
    expect(container.querySelector(".text-danger")).toBeNull();
  });

  it("PROJECTED stat carries 'projected end-of-month' sublabel and is muted", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "300", forecast_expense: "900" },
        })}
      />,
    );
    expect(screen.getByText(/projected end-of-month/i)).toBeInTheDocument();
    const projectedLabel = screen.getByText(/^PROJECTED$/);
    const projectedValue = projectedLabel.parentElement?.querySelectorAll("p")[1];
    expect(projectedValue?.className).toMatch(/text-text-muted/);
  });
});

describe("OnTrackTile — variance and column labels", () => {
  it("renders Variance with brass + 'under plan' sublabel when projection is favorable", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "300", forecast_expense: "850" },
        })}
      />,
    );
    expect(screen.getByText(/^\+/)).toHaveClass("text-accent");
    expect(screen.getByText(/under plan/i)).toBeInTheDocument();
  });

  it("renders Variance with danger + 'over plan' sublabel when actual spending is unfavorable", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "1200", forecast_expense: "1200" },
        })}
      />,
    );
    const variance = screen.getByText(/^−/);
    expect(variance).toHaveClass("text-danger");
    expect(screen.getByText(/over plan/i)).toBeInTheDocument();
  });

  it("uses 'Projected spend' (not 'end-of-period balance') and renders four columns", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "500", forecast_expense: "950" },
        })}
      />,
    );
    expect(screen.getByText(/^PLAN$/)).toBeInTheDocument();
    expect(screen.getByText(/^SPENT SO FAR$/)).toBeInTheDocument();
    expect(screen.getByText(/^VARIANCE$/)).toBeInTheDocument();
    expect(screen.getByText(/^PROJECTED$/)).toBeInTheDocument();
    expect(screen.queryByText(/end.of.period balance/i)).not.toBeInTheDocument();
  });
});

describe("OnTrackTile — degraded states", () => {
  it("no-plan state: suppresses Spent so far (no source independent of projection) and shows the Set-one-up CTA", () => {
    render(<OnTrackTile {...defaults({ forecastPlan: null, projection: null })} />);
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
    expect(screen.getByText(/No plan for this period\. Set one up/)).toBeInTheDocument();
    // Spent so far renders the muted em-dash placeholder, not a number
    const spentLabel = screen.getByText(/^SPENT SO FAR$/);
    const spentValue = spentLabel.parentElement?.querySelectorAll("p")[1];
    expect(spentValue?.textContent).toBe("—");
  });

  it("projection-fail state: plan stays, Spent/Variance/Projected suppress, retry button visible", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: null,
          projectionFailed: true,
        })}
      />,
    );
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
    expect(screen.getByText(/Projection unavailable\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // Plan column shows real value
    expect(screen.getByText(/PLAN$/i)).toBeInTheDocument();
    // Spent so far is suppressed — em-dash placeholder
    const spentLabel = screen.getByText(/^SPENT SO FAR$/);
    const spentValue = spentLabel.parentElement?.querySelectorAll("p")[1];
    expect(spentValue?.textContent).toBe("—");
  });

  it("projection-fail state: clicking Retry calls onRetryProjection", () => {
    const onRetry = vi.fn();
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projectionFailed: true,
          onRetryProjection: onRetry,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("future period: shows Plan ahead CTA, suppresses verdict + variance + projected", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          isFuturePeriod: true,
        })}
      />,
    );
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /plan ahead/i })).toBeInTheDocument();
  });
});

describe("OnTrackTile — past period", () => {
  it("uses executed_expense (not forecast_expense) for the verdict on closed periods", () => {
    // executed_expense alone = 1100/1000 = 1.10 → OVER BUDGET.
    // forecast_expense (used in current period) would be 800/1000 = 0.80 → ON TRACK.
    // Past-period branch must NOT pick the projection.
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "1100", forecast_expense: "800" },
          isPastPeriod: true,
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/ENDED OVER BUDGET/);
    expect(screen.getByRole("heading", { level: 2 })).not.toHaveTextContent(/^ENDED ON TRACK/);
  });

  it("past + no-plan: renders past-tense non-actionable copy, no Set-one-up CTA", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: null,
          projection: null,
          isPastPeriod: true,
        })}
      />,
    );
    expect(screen.getByText(/No plan was set for this period\./)).toBeInTheDocument();
    expect(screen.queryByText(/Set one up/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^This period$/)).not.toBeInTheDocument();
    expect(screen.getByText(/^Past period$/)).toBeInTheDocument();
  });

  it("renders ENDED ON TRACK when final actual spending is comfortably under plan", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: { total_planned_expense: "561.86" },
          projection: { executed_expense: "400", forecast_expense: "400" },
          isPastPeriod: true,
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(/^ENDED ON TRACK/);
  });

  it("renders FINAL SPENT column and suppresses PROJECTED column", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "950", forecast_expense: "950" },
          isPastPeriod: true,
        })}
      />,
    );
    expect(screen.getByText(/^FINAL SPENT$/)).toBeInTheDocument();
    expect(screen.queryByText(/^PROJECTED$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^SPENT SO FAR$/)).not.toBeInTheDocument();
  });
});

describe("OnTrackTile — verdict icon (lucide, not unicode)", () => {
  it("ON TRACK renders a lucide Check icon (svg with aria-hidden)", () => {
    const { container } = render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "100", forecast_expense: "500" },
        })}
      />,
    );
    const svg = container.querySelector('h2 svg[aria-hidden="true"]');
    expect(svg).toBeInTheDocument();
    expect(svg?.classList.contains("lucide")).toBe(true);
  });

  it("OVER BUDGET renders a lucide AlertTriangle icon", () => {
    const { container } = render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "1300", forecast_expense: "1300" },
        })}
      />,
    );
    const svg = container.querySelector('h2 svg[aria-hidden="true"]');
    expect(svg).toBeInTheDocument();
    expect(svg?.classList.contains("lucide-triangle-alert")).toBe(true);
  });
});
