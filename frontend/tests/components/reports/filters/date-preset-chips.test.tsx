import { fireEvent, render, screen } from "@testing-library/react";

import DatePresetChips, {
  buildPresetRanges,
} from "@/components/reports/filters/DatePresetChips";
import { matchPreset } from "@/lib/reports/date-presets";

describe("DatePresetChips", () => {
  it("renders the six preset chips (incl. Next cycle)", () => {
    render(<DatePresetChips value={undefined} onChange={() => {}} />);
    expect(screen.getByTestId("date-preset-this_month")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-last_month")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-ytd")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-last_12_months")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-next_cycle")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-custom")).toBeInTheDocument();
  });

  // TBD-759: calendar presets persist as a relative TOKEN, never an
  // absolute window, so a report saved in one month still reads (and
  // queries) the current month after the calendar rolls over.
  it.each([
    ["this_month"],
    ["last_month"],
    ["ytd"],
    ["last_12_months"],
  ] as const)("writes only {preset:'%s'} (NOT absolute dates) when clicked", (key) => {
    const now = new Date(2026, 4, 15); // May 15, 2026
    const onChange = vi.fn();

    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId(`date-preset-${key}`));

    expect(onChange).toHaveBeenCalledWith({ preset: key });
    const written = onChange.mock.calls[0][0];
    expect(written.start).toBeUndefined();
    expect(written.end).toBeUndefined();
  });

  it("buildPresetRanges still computes each calendar window from now", () => {
    const ranges = buildPresetRanges(new Date(2026, 4, 15)); // May 15, 2026
    expect(ranges.this_month).toEqual({ start: "2026-05-01", end: "2026-05-31" });
    expect(ranges.last_month).toEqual({ start: "2026-04-01", end: "2026-04-30" });
    expect(ranges.ytd).toEqual({ start: "2026-01-01", end: "2026-05-15" });
    expect(ranges.last_12_months).toEqual({ start: "2025-05-01", end: "2026-05-15" });
  });

  describe("after the month rolls over (fake clock)", () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ["Date"] });
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("a value saved as This month in May is still This month in June", () => {
      vi.setSystemTime(new Date(2026, 4, 31, 12)); // May 31, 2026
      const onChange = vi.fn();
      const { unmount } = render(
        <DatePresetChips value={undefined} onChange={onChange} />,
      );
      fireEvent.click(screen.getByTestId("date-preset-this_month"));
      const saved = onChange.mock.calls[0][0];
      unmount();

      vi.setSystemTime(new Date(2026, 5, 1, 9)); // June 1, 2026
      render(<DatePresetChips value={saved} onChange={() => {}} />);
      expect(
        screen.getByTestId("date-preset-this_month").getAttribute("aria-pressed"),
      ).toBe("true");
      expect(
        screen.getByTestId("date-preset-last_month").getAttribute("aria-pressed"),
      ).toBe("false");
      expect(screen.queryByTestId("date-preset-from")).not.toBeInTheDocument();
    });
  });

  it("opens the date inputs when 'Custom' is clicked", () => {
    const onChange = vi.fn();
    render(<DatePresetChips value={undefined} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("date-preset-custom"));
    // Custom path doesn't auto-fill a range; it just enables the inputs.
    expect(onChange).toHaveBeenCalledWith({});
  });

  it("marks the active preset with aria-pressed", () => {
    const now = new Date(2026, 4, 15);
    render(
      <DatePresetChips value={{ preset: "this_month" }} onChange={() => {}} now={now} />,
    );
    expect(
      screen.getByTestId("date-preset-this_month").getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("writes only {preset:'next_cycle'} (NOT absolute dates) when Next cycle is clicked", () => {
    const now = new Date(2026, 4, 15);
    const onChange = vi.fn();
    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId("date-preset-next_cycle"));
    expect(onChange).toHaveBeenCalledWith({ preset: "next_cycle" });
    // The written value carries no absolute window — the backend resolves it.
    const written = onChange.mock.calls[0][0];
    expect(written.start).toBeUndefined();
    expect(written.end).toBeUndefined();
  });

  it("gives the Next cycle chip a precise accessible name that contains the visible label", () => {
    render(<DatePresetChips value={undefined} onChange={() => {}} />);
    const chip = screen.getByTestId("date-preset-next_cycle");
    // WCAG 2.5.3: the accessible name must contain the visible "Next cycle".
    expect(chip).toHaveTextContent("Next cycle");
    expect(chip).toHaveAttribute("aria-label", "Next cycle (next billing cycle)");
    expect(chip).toHaveAttribute("title", "Next cycle (next billing cycle)");
  });

  it("marks the Next cycle chip active for a {preset:'next_cycle'} value", () => {
    render(
      <DatePresetChips value={{ preset: "next_cycle" }} onChange={() => {}} />,
    );
    expect(
      screen.getByTestId("date-preset-next_cycle").getAttribute("aria-pressed"),
    ).toBe("true");
    // No other chip lights up, and the absolute date inputs stay hidden.
    expect(
      screen.getByTestId("date-preset-this_month").getAttribute("aria-pressed"),
    ).toBe("false");
    expect(screen.queryByTestId("date-preset-from")).not.toBeInTheDocument();
  });

  it("matchPreset returns 'next_cycle' for a token value, before the empty-range guard", () => {
    const now = new Date(2026, 4, 15);
    const ranges = buildPresetRanges(now);
    // Token-only value (no start/end) — must still resolve to next_cycle.
    expect(matchPreset({ preset: "next_cycle" }, ranges)).toBe("next_cycle");
    // A legacy saved absolute window still matches by its dates (no backfill).
    expect(matchPreset(ranges.this_month, ranges)).toBe("this_month");
  });

  it("matchPreset reads a calendar token from preset, not from dates", () => {
    // June ranges: a token saved in May must still read this_month.
    const june = buildPresetRanges(new Date(2026, 5, 1));
    expect(matchPreset({ preset: "this_month" }, june)).toBe("this_month");
    expect(matchPreset({ preset: "last_12_months" }, june)).toBe("last_12_months");
  });
});
