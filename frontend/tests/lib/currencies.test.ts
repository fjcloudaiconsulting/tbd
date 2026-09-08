/**
 * Currency picker labels (TBD-325).
 *
 * An organization holds exactly ONE currency — every period aggregate sums
 * without joining `accounts`, so a mixed-currency org would render EUR + USD
 * as a single unlabelled number. The server enforces it; the picker makes it
 * visible, and these are the labels it renders.
 *
 * ⚠ Symbols come from `Intl` at render time rather than a hand-kept table, so
 * these tests pin the RULES rather than a snapshot of 170 strings. A snapshot
 * would go red on an ICU data update in a way nobody could action.
 */
import { describe, expect, it } from "vitest";

import {
  ALL_CURRENCIES,
  COMMON_CURRENCIES,
  OTHER_CURRENCIES,
  currencyLabel,
  currencySymbol,
} from "@/lib/currencies";

describe("currencySymbol", () => {
  it("returns the symbol for currencies that have one", () => {
    expect(currencySymbol("EUR")).toBe("€");
    expect(currencySymbol("USD")).toBe("$");
    expect(currencySymbol("GBP")).toBe("£");
  });

  it("disambiguates the dollar and yen families", () => {
    // The reason a symbol is safe to show at all: these would otherwise be
    // four currencies rendering "$" and two rendering "¥". Measured across all
    // 170 codes, no symbol is shared by two currencies.
    const dollars = ["USD", "CAD", "AUD", "HKD", "NZD", "MXN"].map(currencySymbol);
    expect(new Set(dollars).size).toBe(dollars.length);
    expect(currencySymbol("JPY")).not.toBe(currencySymbol("CNY"));
  });

  it("returns null when the currency has no symbol, rather than echoing the code", () => {
    // `Intl` answers "CHF" for CHF. Returning it would render "CHF CHF".
    expect(currencySymbol("CHF")).toBeNull();
    expect(currencySymbol("SEK")).toBeNull();
    expect(currencySymbol("NOK")).toBeNull();
  });

  it("rejects wordy abbreviations that are not symbols", () => {
    // `Intl` answers "F CFA" / "FCFA" / "CFPF" for these. Next to the code
    // they read as noise ("F CFA XOF") and undo the point of a symbol.
    expect(currencySymbol("XOF")).toBeNull();
    expect(currencySymbol("XAF")).toBeNull();
    expect(currencySymbol("XPF")).toBeNull();
  });

  it("never returns a symbol longer than three characters", () => {
    // The rule, asserted over the whole supported set rather than the few
    // examples above — that is what stops a future ICU change reintroducing a
    // wordy label without anyone noticing.
    for (const code of ALL_CURRENCIES) {
      const symbol = currencySymbol(code);
      if (symbol !== null) expect(symbol.length).toBeLessThanOrEqual(3);
    }
  });

  it("degrades to null on an unsupported code instead of throwing", () => {
    // `Intl` throws RangeError on a bad code. The picker only passes codes
    // from ALL_CURRENCIES, but a throw here would blank the entire list.
    expect(currencySymbol("ZZZ")).toBeNull();
    expect(currencySymbol("")).toBeNull();
  });
});

describe("currencyLabel", () => {
  it("leads with the symbol when there is one", () => {
    expect(currencyLabel("EUR", "Euro")).toBe("€ EUR · Euro");
    expect(currencyLabel("BRL", "Brazilian Real")).toBe("R$ BRL · Brazilian Real");
  });

  it("omits the symbol slot entirely when there is none", () => {
    // Not "  CHF · Swiss Franc" with a hanging gap, and not "CHF CHF".
    expect(currencyLabel("CHF", "Swiss Franc")).toBe("CHF · Swiss Franc");
  });

  it("renders without a name for the long tail", () => {
    expect(currencyLabel("KRW")).toBe("₩ KRW");
    expect(currencyLabel("AED")).toBe("AED");
  });

  it("always contains the code, whatever the symbol does", () => {
    // ⚠ The load-bearing property. The code is what the user matches against
    // the 409 message ("This organization's accounts are in EUR"), so a label
    // that showed only a symbol would break that link.
    for (const code of ALL_CURRENCIES) {
      expect(currencyLabel(code)).toContain(code);
    }
  });
});

describe("the picker's option groups", () => {
  it("covers every supported currency exactly once across both groups", () => {
    // A code in neither group is uncreatable through the UI; a code in both
    // renders twice.
    const common = COMMON_CURRENCIES.map(([code]) => code);
    const all = [...common, ...OTHER_CURRENCIES];
    expect(new Set(all).size).toBe(all.length);
    expect(new Set(all)).toEqual(new Set(ALL_CURRENCIES));
  });

  it("offers nothing the backend would reject", () => {
    // The backend set is fenced against this file by
    // backend/tests/test_currency_list_frontend_contract.py; this is the
    // frontend-side half of that pair.
    for (const [code] of COMMON_CURRENCIES) {
      expect(ALL_CURRENCIES).toContain(code);
    }
  });
});
