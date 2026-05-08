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
  // Bug history: a fully-pending month (executed=0) used to read as OVER
  // BUDGET because projected expense exceeded the plan. Verdict anchors
  // on settled spending only — projection is supporting info.

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
    const expectedLabel = screen.getByText(/^Expected spending$/i);
    const expectedValue = expectedLabel.parentElement?.querySelectorAll("p")[1];
    expect(expectedValue?.textContent).toMatch(/1,050/);
    expect(expectedValue?.className).toMatch(/text-text-muted/);
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
});

describe("OnTrackTile — simplified column set", () => {
  it("current period with plan + projection renders three plain-language columns", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "500", forecast_expense: "950" },
        })}
      />,
    );
    expect(screen.getByText(/^Planned spending$/i)).toBeInTheDocument();
    expect(screen.getByText(/^Spent so far$/i)).toBeInTheDocument();
    expect(screen.getByText(/^Expected spending$/i)).toBeInTheDocument();
  });

  it("does not surface VARIANCE, source labels, or technical (?) markers", () => {
    const { container } = render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "500", forecast_expense: "950" },
        })}
      />,
    );
    expect(screen.queryByText(/^VARIANCE$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^PROJECTED$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^under plan$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^over plan$/i)).not.toBeInTheDocument();
    expect(container.textContent).not.toContain("(?)");
  });

  it("renders a single 'View forecast details' link routing to /forecast-plans", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "500", forecast_expense: "950" },
        })}
      />,
    );
    const link = screen.getByRole("link", { name: /view forecast details/i });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "/forecast-plans");
  });
});

describe("OnTrackTile — degraded states", () => {
  it("no-plan state: suppresses Spent so far and shows the Set-one-up CTA", () => {
    render(<OnTrackTile {...defaults({ forecastPlan: null, projection: null })} />);
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
    expect(screen.getByText(/No plan for this period\. Set one up/)).toBeInTheDocument();
    const spentLabel = screen.getByText(/^Spent so far$/i);
    const spentValue = spentLabel.parentElement?.querySelectorAll("p")[1];
    expect(spentValue?.textContent).toBe("—");
  });

  it("projection-fail state: plan stays, retry button visible, no verdict heading", () => {
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
    expect(screen.getByText(/Forecast unavailable\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    const spentLabel = screen.getByText(/^Spent so far$/i);
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

  it("future period: shows Plan ahead CTA, suppresses verdict + Expected spending", () => {
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
    expect(screen.queryByText(/^Expected spending$/i)).not.toBeInTheDocument();
  });
});

describe("OnTrackTile — past period", () => {
  it("uses executed_expense (not forecast_expense) for the verdict on closed periods", () => {
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

  it("renders Final spent, no Expected spending column", () => {
    render(
      <OnTrackTile
        {...defaults({
          forecastPlan: PLAN_1000,
          projection: { executed_expense: "950", forecast_expense: "950" },
          isPastPeriod: true,
        })}
      />,
    );
    expect(screen.getByText(/^Final spent$/i)).toBeInTheDocument();
    expect(screen.queryByText(/^Expected spending$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Spent so far$/i)).not.toBeInTheDocument();
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
