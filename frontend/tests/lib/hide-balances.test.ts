/**
 * Hide balances: the mask itself (TBD-527, fences F1 + F2).
 *
 * The mask is a FIXED-LENGTH glyph run with no sign, so neither the magnitude
 * (digit count) nor the direction of a figure leaks through it. `toEditAmount`
 * is deliberately untouched: it seeds controlled number inputs, and masking it
 * would save the mask.
 */
import { formatAmount, formatMoney, isBalancesHidden, maskMoneyText, setBalancesHidden, subscribeBalancesHidden, toEditAmount } from "@/lib/format";
import { formatMeasureValue } from "@/lib/reports/series";

const MASK = "•••••";

beforeEach(() => {
  setBalancesHidden(false);
});

describe("F1: formatAmount / formatMoney mask", () => {
  it("a hidden figure carries neither its length nor its sign", () => {
    setBalancesHidden(true);
    expect(formatAmount(1)).toBe(MASK);
    expect(formatAmount(-1e9)).toBe(formatAmount(1));
    expect(formatAmount("7373.37")).toBe(MASK);
  });

  it("formatMoney keeps the currency symbol but not the sign", () => {
    setBalancesHidden(true);
    expect(formatMoney(7373.37, "EUR")).toBe(`€${MASK}`);
    expect(formatMoney(-7373.37, "EUR")).toBe(`€${MASK}`);
    expect(formatMoney(12, null)).toBe(MASK);
  });

  it("toEditAmount is never masked (it seeds a form input)", () => {
    setBalancesHidden(true);
    expect(toEditAmount("19.99")).toBe("19.99");
  });

  it("unhidden output is the ordinary figure", () => {
    expect(formatAmount(1)).not.toBe(MASK);
    expect(formatMoney(-13.14, "EUR")).toMatch(/^-€13/);
  });

  it("persists to localStorage and a fresh module reads it back", async () => {
    setBalancesHidden(true);
    expect(window.localStorage.getItem("tbd-hide-balances")).toBe("1");
    vi.resetModules();
    const fresh = await import("@/lib/format");
    expect(fresh.isBalancesHidden()).toBe(true);
  });

  it("a throwing localStorage means unhidden, not a crash", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    try {
      vi.resetModules();
      const fresh = await import("@/lib/format");
      expect(fresh.isBalancesHidden()).toBe(false);
      expect(fresh.formatAmount(5)).not.toBe(MASK);
      // A write that throws still flips the in-memory flag.
      fresh.setBalancesHidden(true);
      expect(fresh.formatAmount(5)).toBe(MASK);
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });

  it("notifies subscribers on change and stops after unsubscribe", () => {
    const fn = vi.fn();
    const unsubscribe = subscribeBalancesHidden(fn);
    setBalancesHidden(true);
    expect(isBalancesHidden()).toBe(true);
    expect(fn).toHaveBeenCalledTimes(1);
    unsubscribe();
    setBalancesHidden(false);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe("F2: formatMeasureValue (widget cells, tooltips, axis ticks)", () => {
  it("masks currency AND number formats, leaves percent visible", () => {
    setBalancesHidden(true);
    expect(formatMeasureValue(7373.37, "currency", "EUR")).toBe(`€${MASK}`);
    // "number" is where a currency measure lands when the catalog fails or a
    // shared axis mixes formats, so it must be masked too.
    expect(formatMeasureValue(7373.37, "number")).toBe(MASK);
    expect(formatMeasureValue(42.5, "percent")).toBe("42.5%");
  });

  it("unhidden, number keeps its grouped figure", () => {
    expect(formatMeasureValue(7373.37, "number")).toBe((7373.37).toLocaleString());
  });
});

describe("maskMoneyText: amounts inside server-built text", () => {
  const M = "•••••";
  const cases: Array<[string, string]> = [
    // transaction_service: Numeric(12,2) old balance, request-Decimal target
    ["Balance adjustment: 7373.37 -> 7400.00", `Balance adjustment: ${M} -> ${M}`],
    ["Balance adjustment: -120.50 -> 7400", `Balance adjustment: ${M} -> ${M}`],
    ["Balance adjustment: 0.00 -> -50.5 (deposit)", `Balance adjustment: ${M} -> ${M} (deposit)`],
    // budget_draft_service `{projected:.2f}`
    ["Based on about 7373.37 per month over the last 3 months.", `Based on about ${M} per month over the last 3 months.`],
    // budget_rebalance_service `{-delta:.2f}`
    ["Freeing 7373.37 of projected surplus", `Freeing ${M} of projected surplus`],
    // scenario_engine `_q(delta)`
    [
      "Raise the monthly contribution by about 7373.37 to close the gap to the real-terms target.",
      `Raise the monthly contribution by about ${M} to close the gap to the real-terms target.`,
    ],
    // cc_statement_close `f"{owed:,.2f}"`
    ["Your Visa statement closed. 17,373.37 EUR is due on 2026-10-01.", `Your Visa statement closed. ${M} EUR is due on 2026-10-01.`],
  ];

  it.each(cases)("masks %j", (input, expected) => {
    setBalancesHidden(true);
    expect(maskMoneyText(input)).toBe(expected);
  });

  it("leaves text without an amount, dates and percentages alone", () => {
    setBalancesHidden(true);
    for (const plain of ["Groceries at Lidl", "Uber 2 trips", "usage reached 80%", "rate 12.50%", "due 2026-10-01"]) {
      expect(maskMoneyText(plain)).toBe(plain);
    }
  });

  it("returns the text unchanged when balances are shown", () => {
    expect(maskMoneyText(cases[0][0])).toBe(cases[0][0]);
  });
});
