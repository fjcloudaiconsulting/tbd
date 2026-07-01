import { describe, it, expect } from "vitest";

import {
  currencySymbol,
  formatMeasureValue,
  reportCurrency,
} from "@/lib/reports/series";
import { formatAmount } from "@/lib/format";

describe("formatMeasureValue", () => {
  it("currency with no code → grouped, 2 decimals, no currency symbol", () => {
    // Compare against formatAmount (the same fn the app uses) rather than a
    // hardcoded "1,234.56" so the test is locale-independent. With no org
    // currency known, formatting degrades gracefully to a bare grouped
    // amount.
    const out = formatMeasureValue(1234.56, "currency");
    expect(out).toBe(formatAmount(1234.56));
    expect(out).not.toContain("$");
    expect(out).not.toContain("€");
  });

  it("currency with a code → org symbol prefix + grouped amount", () => {
    expect(formatMeasureValue(1234.56, "currency", "EUR")).toBe(
      `€${formatAmount(1234.56)}`,
    );
    expect(formatMeasureValue(1234.56, "currency", "USD")).toBe(
      `$${formatAmount(1234.56)}`,
    );
    expect(formatMeasureValue(1234.56, "currency", "GBP")).toBe(
      `£${formatAmount(1234.56)}`,
    );
  });

  it("currency with an unknown code → padded ISO code prefix", () => {
    expect(formatMeasureValue(1234.56, "currency", "CHF")).toBe(
      `CHF ${formatAmount(1234.56)}`,
    );
  });

  it("currency code is ignored for number/percent formats", () => {
    expect(formatMeasureValue(1234, "number", "EUR")).toBe(
      (1234).toLocaleString(),
    );
    expect(formatMeasureValue(12.3, "percent", "EUR")).toBe("12.3%");
  });

  it("percent → one-decimal percentage (toFixed is locale-independent)", () => {
    expect(formatMeasureValue(12.3, "percent")).toBe("12.3%");
    expect(formatMeasureValue(12.34, "percent")).toBe("12.3%");
    expect(formatMeasureValue(0, "percent")).toBe("0.0%");
  });

  it("number → grouped via toLocaleString", () => {
    expect(formatMeasureValue(1234, "number")).toBe((1234).toLocaleString());
    expect(formatMeasureValue(1234567, "number")).toBe((1234567).toLocaleString());
  });

  it("handles zero and negative values", () => {
    expect(formatMeasureValue(0, "currency")).toBe(formatAmount(0));
    expect(formatMeasureValue(-1234.5, "currency")).toBe(formatAmount(-1234.5));
    expect(formatMeasureValue(-1234.5, "currency", "EUR")).toBe(
      `€${formatAmount(-1234.5)}`,
    );
    expect(formatMeasureValue(-50, "number")).toBe((-50).toLocaleString());
  });

  it('returns "" for non-finite values (never a literal "NaN")', () => {
    expect(formatMeasureValue(NaN, "currency")).toBe("");
    expect(formatMeasureValue(NaN, "currency", "EUR")).toBe("");
    expect(formatMeasureValue(Infinity, "number")).toBe("");
    expect(formatMeasureValue(Number(undefined), "percent")).toBe("");
  });
});

describe("currencySymbol", () => {
  it("maps known ISO codes to symbols", () => {
    expect(currencySymbol("EUR")).toBe("€");
    expect(currencySymbol("USD")).toBe("$");
    expect(currencySymbol("GBP")).toBe("£");
  });

  it("falls back to a padded ISO code for unknown currencies", () => {
    expect(currencySymbol("CHF")).toBe("CHF ");
  });

  it("returns an empty string for missing codes", () => {
    expect(currencySymbol(undefined)).toBe("");
    expect(currencySymbol(null)).toBe("");
    expect(currencySymbol("")).toBe("");
  });
});

describe("reportCurrency", () => {
  it("single distinct currency → that currency", () => {
    expect(reportCurrency([{ currency: "EUR" }])).toBe("EUR");
    // Duplicate of the same currency across accounts is still single-currency.
    expect(
      reportCurrency([{ currency: "EUR" }, { currency: "EUR" }]),
    ).toBe("EUR");
  });

  it("more than one distinct currency → undefined (no symbol shown)", () => {
    // Mixed-currency org: rather than mislabel every widget with the first
    // account's symbol, suppress the symbol so widgets render raw numbers.
    expect(
      reportCurrency([{ currency: "EUR" }, { currency: "USD" }]),
    ).toBeUndefined();
  });

  it("ignores accounts without a currency when counting distinct", () => {
    // A single real currency plus currency-less accounts stays single-currency.
    expect(
      reportCurrency([{ currency: undefined }, { currency: "GBP" }]),
    ).toBe("GBP");
    expect(
      reportCurrency([{ currency: null }, { currency: "USD" }, { currency: null }]),
    ).toBe("USD");
    // Two distinct real currencies amid currency-less accounts still suppress.
    expect(
      reportCurrency([
        { currency: null },
        { currency: "USD" },
        { currency: "EUR" },
      ]),
    ).toBeUndefined();
  });

  it("returns undefined when no account currency is available", () => {
    expect(reportCurrency(undefined)).toBeUndefined();
    expect(reportCurrency(null)).toBeUndefined();
    expect(reportCurrency([])).toBeUndefined();
    expect(reportCurrency([{ currency: null }])).toBeUndefined();
  });
});
