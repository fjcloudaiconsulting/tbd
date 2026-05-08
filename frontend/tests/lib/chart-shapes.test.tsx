import { render } from "@testing-library/react";

import {
  BudgetSpentBarShape,
  budgetSpentBarRadii,
} from "@/lib/chart-shapes";

describe("budgetSpentBarRadii", () => {
  // The whole point of the shared shape is that BOTH Dashboard
  // (spent + remaining stack) and Budgets (spent + remaining + over
  // stack) render identically rounded edges at high utilization.
  it("rounds all four corners when nothing trails the spent segment", () => {
    // Exactly 100% on Dashboard: remaining=0, over absent.
    expect(budgetSpentBarRadii({ spent: 100, remaining: 0 }, 200, 24))
      .toEqual({ left: 4, right: 4 });

    // Exactly 100% on Budgets: remaining=0, over=0.
    expect(budgetSpentBarRadii({ spent: 100, remaining: 0, over: 0 }, 200, 24))
      .toEqual({ left: 4, right: 4 });
  });

  it("squares the right edge whenever a remaining segment trails", () => {
    // Dashboard at 50%: remaining > 0 → right edge squared so the
    // remaining bar butts up cleanly.
    expect(budgetSpentBarRadii({ spent: 50, remaining: 50 }, 100, 24))
      .toEqual({ left: 4, right: 0 });
  });

  it("squares the right edge whenever an over segment trails", () => {
    // Budgets at 120%: spent saturates the budget, over carries the
    // overflow → right edge squared so the over bar butts up cleanly.
    expect(budgetSpentBarRadii({ spent: 100, remaining: 0, over: 20 }, 100, 24))
      .toEqual({ left: 4, right: 0 });
  });

  it("clamps the radius for very narrow or very short bars", () => {
    // The shape can't ask for r=4 if the bar is only 4px tall — it
    // would fold in on itself. We clamp to half-height / half-width.
    expect(budgetSpentBarRadii({ remaining: 0 }, 100, 4))
      .toEqual({ left: 2, right: 2 });
    expect(budgetSpentBarRadii({ remaining: 0 }, 6, 24))
      .toEqual({ left: 3, right: 3 });
  });

  it("treats a missing payload as a full row (defensive)", () => {
    // Recharts has been known to call shape with undefined payload
    // during animation init; we render a closed rounded rect rather
    // than crashing.
    expect(budgetSpentBarRadii(undefined, 100, 24))
      .toEqual({ left: 4, right: 4 });
  });
});

describe("BudgetSpentBarShape", () => {
  it("returns null for zero-area bars (recharts emits these mid-animation)", () => {
    const { container: emptyW } = render(
      <svg>
        <BudgetSpentBarShape x={0} y={0} width={0} height={24} fill="#000" />
      </svg>,
    );
    expect(emptyW.querySelector("path")).toBeNull();

    const { container: emptyH } = render(
      <svg>
        <BudgetSpentBarShape x={0} y={0} width={100} height={0} fill="#000" />
      </svg>,
    );
    expect(emptyH.querySelector("path")).toBeNull();
  });

  it("renders a rounded path at full row utilization", () => {
    const { container } = render(
      <svg>
        <BudgetSpentBarShape
          x={0}
          y={0}
          width={200}
          height={24}
          fill="#abc"
          payload={{ spent: 100, remaining: 0, over: 0 }}
        />
      </svg>,
    );
    const path = container.querySelector("path");
    expect(path).not.toBeNull();
    const d = path!.getAttribute("d") ?? "";
    // Both right corners use a quadratic curve when full-row → two Q
    // commands instead of straight H/V on the right side.
    const qCount = (d.match(/Q /g) ?? []).length;
    expect(qCount).toBe(4);
    expect(path!.getAttribute("fill")).toBe("#abc");
  });

  it("renders a half-rounded path when a trailing segment is present", () => {
    const { container } = render(
      <svg>
        <BudgetSpentBarShape
          x={0}
          y={0}
          width={120}
          height={24}
          fill="#def"
          payload={{ spent: 60, remaining: 60, over: 0 }}
        />
      </svg>,
    );
    const path = container.querySelector("path");
    expect(path).not.toBeNull();
    const d = path!.getAttribute("d") ?? "";
    // Left corners only → two Q commands.
    const qCount = (d.match(/Q /g) ?? []).length;
    expect(qCount).toBe(2);
  });
});
