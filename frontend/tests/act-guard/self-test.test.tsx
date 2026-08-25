/**
 * The gate's own permanent injected defect. TBD-393.
 *
 * THIS FILE IS SUPPOSED TO EMIT AN act() WARNING. Do not "fix" it.
 *
 * The DoD asks that the gate be verified by injecting a new unwrapped state
 * update and confirming it goes red. Done once, that verifies the gate on the
 * day it lands and never again. `ActCanary` makes it permanent: the baseline
 * pins this file at exactly 1, so the gate re-proves itself on every run.
 *
 * It fences the whole chain in two independent links:
 *
 *   link 1 -- the assertion below: the console patch is installed BY SETUP,
 *             and the matcher understands React's real `%s` argument shape.
 *   link 2 -- the `ActCanary: 1` entry in `act-baseline.json`: worker ->
 *             afterAll meta -> reporter -> artifact -> judge. That entry can
 *             only read 1 if every hop works.
 *
 * NOTE THE IMPORTS. This file imports the PURE matcher (`./classify`) and
 * never `./counter`. Importing the counter would install the console patch as
 * a side effect of this file's own import, so the canary would still pass
 * with the `vitest.setup.ts` import deleted -- silently defeating link 1. The
 * tally is therefore read through `globalThis.__actGuardCounts`, which exists
 * only when setup loaded the counter.
 */

import React from "react";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { classify, UNNAMED, UNPARSED } from "./classify";

function ActCanary() {
  const [n, setN] = React.useState(0);
  React.useEffect(() => {
    // Resolves as a microtask AFTER the synchronous render commits, so the
    // update lands with no act scope open. `setN(1)` must genuinely change
    // state -- `setN((v) => v)` is bailed out by React's eager-state
    // comparison and schedules nothing, so it would emit no warning and the
    // canary would silently stop canarying.
    void Promise.resolve().then(() => setN(1));
  }, []);
  return <span data-testid="canary">{n}</span>;
}

describe("act guard canary", () => {
  it("emits exactly one unwrapped act() warning that the matcher parses", async () => {
    const readCounts = globalThis.__actGuardCounts;
    expect(
      readCounts,
      "globalThis.__actGuardCounts is missing: tests/act-guard/counter.ts was " +
        "not loaded by vitest.setup.ts, so NOTHING in the suite is counting " +
        "act() warnings and the gate is blind",
    ).toBeTypeOf("function");

    render(<ActCanary />);
    // Let the microtask land OUTSIDE any act scope. `waitFor` would wrap it
    // in one and defeat the point of the fixture.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(readCounts!().get("ActCanary")).toBe(1);
  });
});

describe("classify", () => {
  it("reads the component name from args[1] when React leaves %s unformatted", () => {
    // Measured against React 19.2.5: this is the real shape.
    expect(
      classify([
        "An update to %s inside a test was not wrapped in act(...).\n\n",
        "DashboardDataProvider",
      ]),
    ).toBe("DashboardDataProvider");
  });

  it("falls back to parsing a pre-formatted message", () => {
    expect(
      classify(["An update to DataTab inside a test was not wrapped in act(...)."]),
    ).toBe("DataTab");
  });

  it("misfiles an unrecognised act warning instead of dropping it", () => {
    expect(classify(["something new inside a test was not wrapped in act( ??"])).toBe(
      UNPARSED,
    );
  });

  it("normalises a null component name", () => {
    expect(
      classify(["An update to %s inside a test was not wrapped in act(...).", null]),
    ).toBe(UNNAMED);
  });

  it("ignores console.error calls that are not act warnings", () => {
    expect(classify(["some unrelated error", 42])).toBeNull();
  });
});
