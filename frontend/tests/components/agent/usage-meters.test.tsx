/**
 * Usage per meter (TBD-581 DoD: used, limit and reset per meter). Guards the
 * three forms, and that platform spend is USD cents whatever the org's
 * currency (wrong: org currency, or cents shown as whole units).
 */
import { render, screen } from "@testing-library/react";

import UsageMeters, { meterLine } from "@/components/agent/UsageMeters";

const day = (used: number, limit: number | null) => ({
  used, limit, period: "day" as const, resets_at: limit === 0 ? null : "2026-10-16T00:00:00+00:00",
});

it("renders used, limit and reset; no limit; and a closed meter", () => {
  render(
    <UsageMeters
      usage={{
        "assistant.turns": day(12, 100),
        "mcp.calls": { used: 3, limit: null, period: "month", resets_at: "2026-11-01T00:00:00+00:00" },
        "platform_ai.tokens": day(0, 0),
      }}
    />,
  );
  expect(screen.getByText(/^12 of 100 today, resets (16 Oct|Oct 16)$/)).toBeInTheDocument();
  expect(screen.getByText("3 used this month, no limit")).toBeInTheDocument();
  expect(screen.getByText("Not in your plan")).toBeInTheDocument();
});

it("shows platform spend as US dollars from cents, and marks a full meter", () => {
  expect(meterLine("platform_ai.cents", { used: 250, limit: 500, period: "month", resets_at: "2026-11-01T00:00:00+00:00" }))
    .toMatch(/^\$2\.50 of \$5\.00 this month, resets (1 Nov|Nov 1)$/);
  render(<UsageMeters usage={{ "assistant.turns": day(100, 100) }} />);
  expect(screen.getByText("Limit reached")).toBeInTheDocument();
});

it("a reset at UTC midnight shows that date, not the local day before", () => {
  const tz = process.env.TZ;
  process.env.TZ = "America/Sao_Paulo"; // UTC-3: local time would read Oct 15
  try {
    expect(meterLine("mcp.calls", day(1, 5))).toMatch(/16 Oct|Oct 16/);
  } finally {
    process.env.TZ = tz;
  }
});
