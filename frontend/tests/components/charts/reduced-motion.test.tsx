import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import {
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  XAxis,
  LineChart,
  Line,
  AreaChart,
  Area,
} from "recharts";

/**
 * TBD-428 — behavioural fence on reduced-motion chart animation.
 *
 * ⚠⚠ THIS FILE MUST NOT ``vi.mock("recharts")``. Every other chart test in
 * this repo mocks the library away and asserts on the props handed to a fake
 * ``<Bar>``. That is the right call for those tests and the WRONG call here:
 * the property under test is recharts' OWN behaviour, so a mock makes this
 * file assert nothing at all. If a future change adds the mock "for
 * consistency", this fence silently becomes decoration.
 *
 * ## What is actually true (measured, not assumed)
 *
 * recharts 3.x defaults ``isAnimationActive`` to the string ``"auto"``
 * (``Bar.js`` defaultProps), and ``"auto"`` resolves to
 * ``!Global.isSsr && !prefersReducedMotion`` in ``JavascriptAnimate`` /
 * ``CSSTransitionAnimate``, backed by recharts' own ``usePrefersReducedMotion``
 * subscribing to ``matchMedia("(prefers-reduced-motion: reduce)")``.
 *
 * Verified true for every primitive this app uses: ``Bar``, ``Pie``, ``Area``,
 * ``Line`` and ``Tooltip`` all default to ``"auto"``. ``ErrorBar`` is the one
 * exception in the library -- it defaults to a literal ``true`` -- but it
 * animates through a CSS transition, which ``globals.css:382`` does neutralize,
 * and it is unused here.
 *
 * So a chart that sets ``animationDuration`` and leaves ``isAnimationActive``
 * UNSET — the shape used by ``BudgetBarsWidget``, ``ForecastBarsWidget``,
 * ``BudgetOverviewChart``, ``ForecastPlanChart`` and the dashboard page — is
 * already correct under docs/product/PRODUCT.md's reduced-motion commitment. TBD-428 was
 * filed against a recharts 2.x mental model, where the default was ``true``;
 * this repo has been on ``^3.8.1`` since recharts was introduced.
 *
 * ## The wrong implementations this file kills
 *
 * F1/F4  a recharts upgrade (or a config change) where the default no longer
 *        consults ``prefers-reduced-motion`` -> reduce-motion users get motion.
 * F2/F5  a contributor writing ``isAnimationActive={true}``, which overrides
 *        ``"auto"`` and animates regardless of the user's preference. This is
 *        ALSO a permanent positive control: it proves the assertion in F1/F4
 *        can distinguish "did not animate" from "this harness renders
 *        everything instantly", which is the way a fence like this goes
 *        vacuous.
 * F3/F6  over-correction — disabling animation for EVERYONE (what
 *        ``isAnimationActive={false}`` does) rather than only for users who
 *        asked. TBD-382 did exactly that to the report widgets.
 * F10-F15 the same three shapes for ``<Area>`` and ``<Line>`` (TBD-437).
 *        ⚠ F13 pins a recharts 3.8.1 DEFECT, not a correct state: see its
 *        note, line-geometry.test.tsx, and TBD-528.
 *
 * ## Discriminator
 *
 * First-paint geometry -- specifically, WHETHER A PATH ELEMENT EXISTS. At
 * ``t === 0`` an animating ``<Bar>`` has ``height === 0`` and ``Rectangle``
 * returns ``null``; an animating ``<Pie>`` has ``startAngle === endAngle`` and
 * ``Sector`` returns ``null``. recharts never emits a path with an empty
 * ``d``, so the helper below is an existence check on the path, and that is
 * the honest name for it. Each assertion is paired with a precondition that
 * the chart surface itself rendered, so "nothing rendered at all" cannot
 * satisfy the negative cases. ``rAF`` is
 * stubbed to a no-op throughout so no animation frame ever lands: that keeps
 * the assertion at "first paint", makes the file deterministic, and — because
 * no state update escapes into a later tick — keeps it free of act()
 * warnings, which the TBD-393 gate compares with strict equality in BOTH
 * directions.
 */

function mockMatchMedia(reduce: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduce && query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

const BAR_DATA = [{ name: "a", spent: 80 }];
const TOOLTIP_DATA = [
  { name: "a", spent: 80 },
  { name: "b", spent: 40 },
];
const PIE_DATA = [
  { name: "a", value: 60 },
  { name: "b", value: 40 },
];

/** `active === undefined` reproduces the app's prop shape: a duration, and no
 *  `isAnimationActive`, so recharts' "auto" default is what is under test. */
function renderBar(active?: boolean) {
  render(
    <div data-testid="host">
      <BarChart width={300} height={100} data={BAR_DATA}>
        <Bar
          dataKey="spent"
          fill="#888888"
          animationDuration={220}
          {...(active === undefined ? {} : { isAnimationActive: active })}
        />
      </BarChart>
    </div>,
  );
}

function renderPie(active?: boolean) {
  render(
    <div data-testid="host">
      <PieChart width={200} height={200}>
        <Pie
          data={PIE_DATA}
          dataKey="value"
          animationDuration={220}
          {...(active === undefined ? {} : { isAnimationActive: active })}
        >
          <Cell fill="#888888" />
          <Cell fill="#999999" />
        </Pie>
      </PieChart>
    </div>,
  );
}

/** True when recharts has committed geometry on the first paint, i.e. it is
 *  NOT animating. See the Discriminator note: while animating, recharts
 *  renders no path element at all rather than one with an empty ``d``. */
function hasCommittedGeometry(selector: string): boolean {
  return screen.getByTestId("host").querySelector(selector) !== null;
}

/** The chart itself must have rendered. Without this, "the component threw"
 *  or "a class name changed" would satisfy every ``false`` assertion below
 *  and the negative cases would pass for the wrong reason. */
function expectChartRendered(): void {
  expect(
    screen.getByTestId("host").querySelector(".recharts-surface"),
  ).not.toBeNull();
}

const BAR = ".recharts-bar-rectangle path";
const PIE = ".recharts-pie-sector path";

describe("TBD-428: recharts honours prefers-reduced-motion", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // ⚠⚠ FAKE TIMERS ARE LOAD-BEARING HERE, NOT TIDINESS (TBD-459).
    //
    // recharts 3.x drives its internal store with Redux Toolkit, whose
    // ``autoBatchEnhancer`` schedules notifications through
    // ``createRafWithFallbackTimer``: it arms BOTH ``raf(callback)`` and
    // ``setTimeout(callback, timeout)``, and whichever fires first calls
    // ``cancelAnimationFrame(rafId)`` — a BARE global lookup, resolved when it
    // runs — then clears the other.
    //
    // The rAF stub below returns a handle and never invokes its callback, by
    // design (see the Discriminator note above). So the rAF arm can never fire,
    // ``clearTimeout`` never runs, and the fallback timer is left armed. On a
    // loaded runner it lands AFTER vitest tears the jsdom environment down, at
    // which point ``cancelAnimationFrame`` no longer exists:
    //
    //   ReferenceError: cancelAnimationFrame is not defined
    //     ❯ Timeout.callback @reduxjs/toolkit/src/autoBatchEnhancer.ts:23
    //
    // That is an UNHANDLED error, so vitest fails the whole suite while
    // reporting every test passed — measured on main 2026-08-29, and green on a
    // re-run of the identical commit. Two timers are left pending per render;
    // faking them keeps them off the real event loop, and ``useRealTimers`` in
    // the teardown below discards them. Do not "simplify" this away.
    vi.useFakeTimers();
    // No animation frame ever lands — see the Discriminator note above.
    vi.stubGlobal("requestAnimationFrame", () => 0);
    vi.stubGlobal("cancelAnimationFrame", () => {});
  });

  afterEach(() => {
    // Order matters: drop the faked clock (discarding recharts' still-armed
    // autobatch timers) before restoring the globals it would have reached for.
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("F1 fence: <Bar> with the app's prop shape does not animate under reduce", () => {
    mockMatchMedia(true);
    renderBar();
    expectChartRendered();
    expect(hasCommittedGeometry(BAR)).toBe(true);
  });

  it("F2 fence + positive control: isAnimationActive={true} animates even under reduce", () => {
    mockMatchMedia(true);
    renderBar(true);
    expectChartRendered();
    expect(hasCommittedGeometry(BAR)).toBe(false);
  });

  it("F3 guard: <Bar> still animates for users who did not opt out", () => {
    mockMatchMedia(false);
    renderBar();
    expectChartRendered();
    expect(hasCommittedGeometry(BAR)).toBe(false);
  });

  it("F4 fence: <Pie> with the app's prop shape does not animate under reduce", () => {
    mockMatchMedia(true);
    renderPie();
    expectChartRendered();
    expect(hasCommittedGeometry(PIE)).toBe(true);
  });

  it("F5 fence + positive control: isAnimationActive={true} animates even under reduce", () => {
    mockMatchMedia(true);
    renderPie(true);
    expectChartRendered();
    expect(hasCommittedGeometry(PIE)).toBe(false);
  });

  it("F6 guard: <Pie> still animates for users who did not opt out", () => {
    mockMatchMedia(false);
    renderPie();
    expectChartRendered();
    expect(hasCommittedGeometry(PIE)).toBe(false);
  });
  // ---- Tooltip: recharts' THIRD, separately-written reduced-motion
  // predicate. `TooltipBoundingBox.resolveTransitionProperty` has its own
  // `prefersReducedMotion && isAnimationActive === "auto"` branch, distinct
  // from the one in `JavascriptAnimate` that F1-F6 cover. A recharts upgrade
  // touching only that two-line function would regress every tooltip in the
  // app while F1-F6 and the structural gate stayed green -- which is exactly
  // the shape this whole file exists to prevent. Live on 12+ chart surfaces
  // (app/dashboard, app/transactions, app/accounts, app/categories,
  // app/budgets, app/forecast-plans, SpendingDonutWidget, ...), none of which
  // passes `isAnimationActive`, so all resolve to "auto".

  function renderTooltip(active?: boolean) {
    render(
      <div data-testid="host">
        <BarChart width={300} height={100} data={TOOLTIP_DATA}>
          <XAxis dataKey="name" />
          <Bar dataKey="spent" fill="#888888" isAnimationActive={false} />
          <Tooltip
            defaultIndex={0}
            {...(active === undefined ? {} : { isAnimationActive: active })}
          />
        </BarChart>
      </div>,
    );
    const wrapper = screen
      .getByTestId("host")
      .querySelector(".recharts-tooltip-wrapper") as HTMLElement | null;
    expect(wrapper, "tooltip wrapper did not render").not.toBeNull();
    return wrapper as HTMLElement;
  }

  it("F7 fence: tooltip has no transition under reduce", () => {
    mockMatchMedia(true);
    expect(renderTooltip().style.transition).toBe("");
  });

  it("F8 fence + positive control: isAnimationActive={true} transitions under reduce", () => {
    mockMatchMedia(true);
    expect(renderTooltip(true).style.transition).toBe("transform 400ms ease");
  });

  it("F9 guard: tooltip still transitions for users who did not opt out", () => {
    mockMatchMedia(false);
    expect(renderTooltip().style.transition).toBe("transform 400ms ease");
  });

  // ---- Area and Line (TBD-437). TBD-437 re-enabled animation on the report
  // and scenario Area marks by deleting `isAnimationActive={false}`, so "auto"
  // is now what stands between those charts and a reduced-motion user. The
  // app's Lines stay hard-off (TBD-528, see F13). Neither mark can use the
  // Bar/Pie "no path yet" discriminator: both render their path at t === 0
  // and animate something else.

  const SERIES = [
    { name: "a", v: 10 },
    { name: "b", v: 40 },
    { name: "c", v: 20 },
  ];

  function animationProps(active?: boolean) {
    return {
      animationDuration: 220,
      ...(active === undefined ? {} : { isAnimationActive: active }),
    };
  }

  /** `<Area>` animates by growing a clip rect from width 0, so at first paint
   *  an animating area is fully clipped and a committed one is not. */
  function areaClipWidth(active?: boolean): number {
    render(
      <div data-testid="host">
        <AreaChart width={300} height={100} data={SERIES}>
          <Area dataKey="v" {...animationProps(active)} />
        </AreaChart>
      </div>,
    );
    expectChartRendered();
    const host = screen.getByTestId("host");
    expect(host.querySelector(".recharts-area-area"), "area path did not render").not.toBeNull();
    const rect = host.querySelector(".recharts-area clipPath rect");
    expect(rect, "area animation clip rect did not render").not.toBeNull();
    return Number(rect!.getAttribute("width"));
  }

  it("F10 fence: <Area> with the app's prop shape does not animate under reduce", () => {
    mockMatchMedia(true);
    expect(areaClipWidth()).toBeGreaterThan(0);
  });

  it("F11 fence + positive control: <Area isAnimationActive={true}> animates even under reduce", () => {
    mockMatchMedia(true);
    expect(areaClipWidth(true)).toBe(0);
  });

  it("F12 guard: <Area> still animates for users who did not opt out", () => {
    mockMatchMedia(false);
    expect(areaClipWidth()).toBe(0);
  });

  describe("<Line>", () => {
    // `<Line>` animates by growing `stroke-dasharray` from `0px <length>px`,
    // where the length comes from `SVGPathElement.getTotalLength()`. jsdom
    // does not implement it, and recharts falls back to 0, which makes an
    // animating line (`0px 0px`) indistinguishable from a committed one: every
    // case below would pass whatever recharts did. So the method is supplied.
    // ⚠ This stub is what makes F14/F15 able to FAIL. Without it they go red,
    // not green: that is the positive control doing its job.
    const LENGTH = 500;
    let original: PropertyDescriptor | undefined;

    beforeEach(() => {
      original = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getTotalLength");
      Object.defineProperty(SVGElement.prototype, "getTotalLength", {
        configurable: true,
        value: () => LENGTH,
      });
    });

    afterEach(() => {
      if (original) Object.defineProperty(SVGElement.prototype, "getTotalLength", original);
      else delete (SVGElement.prototype as unknown as Record<string, unknown>).getTotalLength;
    });

    /** The `stroke-dasharray` recharts committed on first paint. */
    function lineDash(active?: boolean): string | null {
      render(
        <div data-testid="host">
          <LineChart width={300} height={100} data={SERIES}>
            <Line dataKey="v" dot={false} {...animationProps(active)} />
          </LineChart>
        </div>,
      );
      expectChartRendered();
      const path = screen.getByTestId("host").querySelector(".recharts-line-curve");
      expect(path, "line path did not render").not.toBeNull();
      return path!.getAttribute("stroke-dasharray");
    }

    /** First frame of the draw-on animation: nothing drawn, a gap the length
     *  of the whole path. */
    const ANIMATING = `0px ${LENGTH}px`;

    it("F13 pin: under reduce, 'auto' does not start the draw-on, but commits the recharts 3.8.1 stale dash", () => {
      // ⚠ NOT an acceptable committed state. Animation resolves OFF, so there
      // is no `0px <length>px` first frame, but recharts 3.8.1 gates its dash
      // override on the RAW prop (`"auto"` is truthy) and builds the dash from
      // the length of the PREVIOUS commit's path, which on mount is 0. A real
      // Line should carry no dash here. Review measured the same bug leaving
      // 46% of a line undrawn after a resize, which is why the app's four
      // Lines stay `isAnimationActive={false}` (line-geometry.test.tsx).
      //
      // Pinned exactly so a recharts bump that fixes it goes red HERE too,
      // pointing at TBD-528 rather than passing silently.
      mockMatchMedia(true);
      expect(lineDash()).toBe("0px 0px");
    });

    it("F14 fence + positive control: <Line isAnimationActive={true}> animates even under reduce", () => {
      mockMatchMedia(true);
      expect(lineDash(true)).toBe(ANIMATING);
    });

    it("F15 guard: <Line> still animates for users who did not opt out", () => {
      mockMatchMedia(false);
      expect(lineDash()).toBe(ANIMATING);
    });
  });
});
