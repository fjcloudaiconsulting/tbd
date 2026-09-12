/**
 * OnTrackTile withholds the VERDICT when money was excluded (TBD-325, PR 2).
 *
 * Spec: specs/2026-09-12-tbd-325-pr2-currency-scope.md.
 *
 * WHAT GOES WRONG WITHOUT THE GUARD
 * ---------------------------------
 * `currency_scope` selects one currency. An org whose USD spending is scoped
 * out has `executed_expense` fall from 5490.75 to 1215.30 (measured), so
 * `pct = executed / plan` lands back inside the ON TRACK band and the tile
 * renders a green "✓ ON TRACK" over spending the scope deleted. That is a
 * WRONG VERDICT in a money product, not a missing one. The three figures are
 * correct for the scoped currency — incomplete, not wrong — so they stay; only
 * the verdict goes.
 *
 * ⚠⚠ THIS FILE IS SEPARATE FROM on-track-tile.test.tsx ON PURPOSE, AND MUST
 * NOT DRIFT INTO ITS TERRITORY. That file pins `executed_expense: "0"` as ON
 * TRACK for a fully-pending month — itself a previously reported bug. The
 * guard here keys on `currency_scope.excluded_account_count`, NEVER on an
 * amount: no arithmetic on the numerator can tell "the scope deleted your
 * spending" from "nothing has settled yet".
 * `renders the verdict for a zero amount when nothing was excluded` below is
 * the assertion that keeps the two from colliding.
 *
 * ⚠ A PROP-LEVEL FENCE IS NOT VACUOUS EVEN THOUGH NO API PATH REACHES IT.
 * As of 2026-09-12 `excluded_account_count > 0` is unreachable through the API
 * (13 scenarios, 24 race trials, every one 0): both account-insert sites are
 * guarded, currency is immutable post-create, legacy multi orgs backfill to
 * NULL. But that is a fact about the BACKEND, and this component's input is a
 * PROP — its contract is "given this prop, render correctly". Spec V7 rules
 * out a `currency_scope` test on a single-currency BACKEND fixture; this is
 * not that.
 *
 * ⚠ `ON TRACK` is a substring of `ENDED ON TRACK`, so every verdict matcher
 * here is anchored, the way the sibling suite does it. The strings are
 * `OVER BUDGET` / `ENDED OVER BUDGET`, never `OVER`.
 */
import { render, screen } from "@testing-library/react";

import OnTrackTile from "@/components/dashboard/OnTrackTile";

const PLAN_500 = { total_planned_expense: "500" };

// Scoped 100/500 = 0.20 (ON TRACK); the naive cross-currency sum is
// 5100/500 = 10.2 (OVER BUDGET). The two land on OPPOSITE sides of the
// rendered band, which is what makes the fixture worth anything — see the
// backend file's module docstring for why "two figures that merely differ" is
// not enough.
const SCOPED = {
  executed_expense: "100",
  forecast_expense: "140",
  currency_scope: {
    currency: "EUR",
    excluded_currencies: ["USD"],
    excluded_account_count: 1,
  },
};

const UNSCOPED = {
  executed_expense: "100",
  forecast_expense: "140",
  currency_scope: {
    currency: "EUR",
    excluded_currencies: [],
    excluded_account_count: 0,
  },
};

function defaults(overrides = {}) {
  return {
    forecastPlan: PLAN_500,
    projection: null,
    projectionFailed: false,
    projectionLoading: false,
    onRetryProjection: vi.fn(),
    isPastPeriod: false,
    isFuturePeriod: false,
    ...overrides,
  };
}

describe("OnTrackTile — partial currency scope", () => {
  it("renders no verdict heading when accounts were excluded", () => {
    render(<OnTrackTile {...defaults({ projection: SCOPED })} />);
    // Kills: the guard dropped. Without it 0.20 renders a green ON TRACK.
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
  });

  it("does not put a verdict string in the region's accessible name", () => {
    render(<OnTrackTile {...defaults({ projection: SCOPED })} />);
    // The aria-label IS the region's accessible name, so a verdict there is
    // the same lie, told only to screen readers — where nobody would find it.
    const tile = screen.getByTestId("on-track-tile");
    const label = tile.getAttribute("aria-label") ?? "";
    for (const verdict of [
      "ON TRACK",
      "WATCH",
      "OVER BUDGET",
      "ENDED ON TRACK",
      "ENDED OVER BUDGET",
    ]) {
      expect(label).not.toContain(verdict);
    }
    expect(label).toBeTruthy();
  });

  it("keeps all three figures — they are incomplete, not wrong", () => {
    render(<OnTrackTile {...defaults({ projection: SCOPED })} />);
    // Kills: suppressing the whole tile. The scoped figures are correct for
    // the currency they cover, and a user with no numbers at all is worse off.
    // ⚠ PIN THE VALUES, not their presence. An earlier cut asserted only
    // `toBeTruthy()`, which a mutant replacing all three with `money(0)`
    // satisfied -- 27/27 green while the tile rendered zeros. "Incomplete, not
    // wrong" is a claim about the NUMBERS, so the numbers are what this
    // asserts.
    //
    // ⚠ `textContent` on the value element, not `getByText` on the figure:
    // the currency symbol and the magnitude are separate nodes, so an anchored
    // text matcher cannot see the whole string.
    for (const [label, expected] of [
      [/^Planned spending$/i, /500/],
      [/^Spent so far$/i, /100/],
      [/^Expected spending$/i, /140/],
    ] as const) {
      const el = screen.getByText(label);
      const value = el.parentElement?.querySelectorAll("p")[1];
      expect(value?.textContent).toMatch(expected);
      // TBD-325 PR 2: inside this branch the currency IS knowable, so the
      // figures carry it. Kills rendering them bare beneath a sentence that
      // names the currency.
      expect(value?.textContent).toContain("€");
    }
  });

  it("says which currency is shown and what was left out", () => {
    render(<OnTrackTile {...defaults({ projection: SCOPED })} />);
    // ⚠ textContent, not getByText: the sentence is split across sibling
    // elements by the interpolated currency and count, and getByText cannot
    // see across a trailing sibling span.
    const explainer = screen.getByTestId("on-track-currency-scope");
    expect(explainer.textContent).toContain("EUR");
    expect(explainer.textContent).toContain("USD");
    expect(explainer.textContent).toMatch(/1 account/);
  });

  it("withholds the verdict on a PAST period too", () => {
    // ⚠ One branch, above the past/current fork. Spec V5 rules out testing the
    // past branch as a separate structure — this asserts the ONE branch covers
    // both, which is the thing a duplicated guard would eventually stop doing.
    render(
      <OnTrackTile {...defaults({ projection: SCOPED, isPastPeriod: true })} />,
    );
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
    expect(screen.getByTestId("on-track-currency-scope")).toBeInTheDocument();
  });
});

describe("OnTrackTile — the guard keys on the SCOPE, never on an amount", () => {
  it("renders the verdict normally when nothing was excluded", () => {
    render(<OnTrackTile {...defaults({ projection: UNSCOPED })} />);
    // ⚠ OVER-REACH CONTROL, not a fence: passes against today's unmodified
    // code. Without it, "never render a verdict" satisfies every assertion
    // above while deleting the tile's entire reason to exist.
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(
      /^ON TRACK/,
    );
    expect(
      screen.queryByTestId("on-track-currency-scope"),
    ).not.toBeInTheDocument();
  });

  it("renders the verdict for a ZERO amount when nothing was excluded", () => {
    // ⚠⚠ THE COLLISION FENCE. on-track-tile.test.tsx:60-79 pins
    // `executed_expense: "0"` as ON TRACK for a fully-pending month — a
    // previously reported bug. A guard written as `executedExpense === 0`
    // re-opens it and would still pass every other test in THIS file. This is
    // the one that goes red for it.
    render(
      <OnTrackTile
        {...defaults({
          projection: { ...UNSCOPED, executed_expense: "0", forecast_expense: "1050" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(
      /^ON TRACK/,
    );
  });

  it("withholds the verdict for a ZERO amount when accounts WERE excluded", () => {
    // The same zero, the other side of the key. Together with the test above,
    // this pins the guard to `excluded_account_count` and to nothing else:
    // no implementation that reads the numerator can satisfy both.
    render(
      <OnTrackTile
        {...defaults({
          projection: { ...SCOPED, executed_expense: "0" },
        })}
      />,
    );
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
  });

  it("renders the verdict when the projection carries no scope at all", () => {
    // `currency_scope` is optional on the wire. A tile that treated `undefined`
    // as "something was excluded" would blank the verdict for every client that
    // has not deployed the new payload yet.
    render(
      <OnTrackTile
        {...defaults({
          projection: { executed_expense: "100", forecast_expense: "140" },
        })}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(
      /^ON TRACK/,
    );
  });
});
